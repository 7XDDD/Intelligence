"""Financial Modeling Prep（FMP）市场数据封装层，基于 stable API。

对外四个接口，供情报报告生成调用：

    quote(symbol)      最近交易日快照（价格、涨跌、成交量、估值）
    profile(symbol)    公司概况（行业、CEO、员工数、业务描述）
    historical(symbol) 日线行情（按日期升序）
    financials(symbol) 三大报表关键科目（income / balance / cashflow）

API key 从环境变量 FMP_API_KEY 读取，未设置时回退到同级目录的 .env。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE_URL = "https://financialmodelingprep.com/stable"

# statement -> stable API 端点
STATEMENTS = {
    "income": "income-statement",
    "balance": "balance-sheet-statement",
    "cashflow": "cash-flow-statement",
}

# statement -> 归一化字段名: FMP 原始字段名
STATEMENT_FIELDS = {
    "income": {
        "revenue": "revenue",
        "gross_profit": "grossProfit",
        "operating_income": "operatingIncome",
        "net_income": "netIncome",
        "eps_diluted": "epsDiluted",
        "ebitda": "ebitda",
    },
    "balance": {
        "total_assets": "totalAssets",
        "total_liabilities": "totalLiabilities",
        "shareholders_equity": "totalStockholdersEquity",
        "cash_and_equivalents": "cashAndCashEquivalents",
        "total_debt": "totalDebt",
    },
    "cashflow": {
        "operating_cash_flow": "operatingCashFlow",
        "capex": "capitalExpenditure",
        "free_cash_flow": "freeCashFlow",
        "net_income": "netIncome",
    },
}


class FMPError(RuntimeError):
    """FMP 请求失败或返回空数据。"""


def _load_dotenv() -> None:
    """把同级 .env 载入 os.environ（已存在的变量不覆盖）。"""
    path = Path(__file__).with_name(".env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def _iso_day(value: date | str) -> str:
    return value.isoformat() if isinstance(value, date) else value


class FMPClient:
    """FMP stable API 的轻客户端。"""

    def __init__(self, api_key: str | None = None, timeout: float = 20.0) -> None:
        if api_key is None:
            _load_dotenv()
            api_key = os.environ.get("FMP_API_KEY")
        if not api_key:
            raise FMPError("缺少 API key：请设置环境变量 FMP_API_KEY 或在 .env 中配置")
        self.api_key = api_key
        self.timeout = timeout

    def _get(self, endpoint: str, **params):
        query = urllib.parse.urlencode({**params, "apikey": self.api_key})
        request = urllib.request.Request(
            f"{BASE_URL}/{endpoint}?{query}", headers={"Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise FMPError(f"{endpoint} 请求失败：HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise FMPError(f"{endpoint} 网络错误：{exc.reason}") from exc

        if isinstance(payload, dict):
            # FMP 用 {"Error Message": "..."} 表示 key 无效、权限不足等
            message = payload.get("Error Message") or payload.get("message") or payload
            raise FMPError(f"{endpoint} 返回错误：{message}")
        if not isinstance(payload, list):
            raise FMPError(f"{endpoint} 返回格式异常：{type(payload).__name__}")
        return payload

    def quote(self, symbol: str) -> dict:
        """最近交易日快照。"""
        rows = self._get("quote", symbol=symbol.upper())
        if not rows:
            raise FMPError(f"{symbol} quote 无数据")
        row = rows[0]
        timestamp = row.get("timestamp")
        return {
            "symbol": row.get("symbol"),
            "name": row.get("name"),
            "price": row.get("price"),
            "change": row.get("change"),
            "change_percent": row.get("changePercentage"),
            "volume": row.get("volume"),
            "previous_close": row.get("previousClose"),
            "open": row.get("open"),
            "day_low": row.get("dayLow"),
            "day_high": row.get("dayHigh"),
            "year_low": row.get("yearLow"),
            "year_high": row.get("yearHigh"),
            "market_cap": row.get("marketCap"),
            "avg50": row.get("priceAvg50"),
            "avg200": row.get("priceAvg200"),
            "exchange": row.get("exchange"),
            "as_of": (
                datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
                if timestamp
                else None
            ),
        }

    def profile(self, symbol: str) -> dict:
        """公司概况。"""
        rows = self._get("profile", symbol=symbol.upper())
        if not rows:
            raise FMPError(f"{symbol} profile 无数据")
        row = rows[0]
        employees = row.get("fullTimeEmployees")
        return {
            "symbol": row.get("symbol"),
            "name": row.get("companyName"),
            "sector": row.get("sector"),
            "industry": row.get("industry"),
            "country": row.get("country"),
            "exchange": row.get("exchangeFullName") or row.get("exchange"),
            "currency": row.get("currency"),
            "ceo": row.get("ceo"),
            "employees": (
                int(employees)
                if isinstance(employees, str) and employees.isdigit()
                else employees
            ),
            "website": row.get("website"),
            "ipo_date": row.get("ipoDate"),
            "market_cap": row.get("marketCap"),
            "beta": row.get("beta"),
            "is_etf": row.get("isEtf"),
            "is_fund": row.get("isFund"),
            "description": row.get("description"),
        }

    def historical(
        self,
        symbol: str,
        start: date | str | None = None,
        end: date | str | None = None,
    ) -> list[dict]:
        """日线行情，按日期升序。默认取最近一年。"""
        end_day = end or date.today()
        start_day = start or (
            end_day - timedelta(days=365) if isinstance(end_day, date) else None
        )
        if start_day is None:
            raise ValueError("start 与 end 同为字符串时，请显式传入 start")

        rows = self._get(
            "historical-price-eod/full",
            symbol=symbol.upper(),
            **{"from": _iso_day(start_day), "to": _iso_day(end_day)},
        )
        bars = [
            {
                "date": row.get("date"),
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
                "volume": row.get("volume"),
                "change": row.get("change"),
                "change_percent": row.get("changePercent"),
                "vwap": row.get("vwap"),
            }
            for row in rows
        ]
        bars.sort(key=lambda bar: bar["date"] or "")
        return bars

    def financials(
        self,
        symbol: str,
        statement: str = "income",
        period: str = "annual",
        limit: int = 5,
    ) -> list[dict]:
        """三大报表的关键科目，按报告期倒序（最新在前）。"""
        if statement not in STATEMENTS:
            raise ValueError(f"statement 仅支持 {sorted(STATEMENTS)}")
        if period not in ("annual", "quarter"):
            raise ValueError("period 仅支持 annual / quarter")

        rows = self._get(
            STATEMENTS[statement],
            symbol=symbol.upper(),
            period=period,
            limit=limit,
        )
        fields = STATEMENT_FIELDS[statement]
        return [
            {
                "symbol": symbol.upper(),
                "date": row.get("date"),
                "fiscal_year": row.get("fiscalYear"),
                "period": row.get("period"),
                "filing_date": row.get("filingDate"),
                "currency": row.get("reportedCurrency"),
                **{key: row.get(source) for key, source in fields.items()},
            }
            for row in rows
        ]


_default_client: FMPClient | None = None


def _client() -> FMPClient:
    global _default_client
    if _default_client is None:
        _default_client = FMPClient()
    return _default_client


def quote(symbol: str) -> dict:
    return _client().quote(symbol)


def profile(symbol: str) -> dict:
    return _client().profile(symbol)


def historical(
    symbol: str,
    start: date | str | None = None,
    end: date | str | None = None,
) -> list[dict]:
    return _client().historical(symbol, start=start, end=end)


def financials(
    symbol: str,
    statement: str = "income",
    period: str = "annual",
    limit: int = 5,
) -> list[dict]:
    return _client().financials(symbol, statement=statement, period=period, limit=limit)


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="拉取单个标的的市场数据并输出 JSON")
    parser.add_argument("symbol")
    parser.add_argument("--days", type=int, default=30, help="历史行情回看天数")
    parser.add_argument("--statement", default="income", choices=sorted(STATEMENTS))
    parser.add_argument("--period", default="annual", choices=["annual", "quarter"])
    parser.add_argument("--limit", type=int, default=4)
    args = parser.parse_args()

    client = FMPClient()
    print(
        json.dumps(
            {
                "quote": client.quote(args.symbol),
                "profile": client.profile(args.symbol),
                "historical": client.historical(
                    args.symbol, start=date.today() - timedelta(days=args.days)
                ),
                "financials": client.financials(
                    args.symbol,
                    statement=args.statement,
                    period=args.period,
                    limit=args.limit,
                ),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    _main()
