"""FMP（Financial Modeling Prep）Key 校验：只验证连通性与行情权限，不打印完整 Key。"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from ..http import HttpClient, HttpError


class FMPError(RuntimeError):
    """FMP 校验失败（Key 缺失、无效、无权限或接口异常）。"""


def mask_key(key: str) -> str:
    """脱敏展示：仅保留前 4 位与后 4 位。"""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}...{key[-4:]}"


def _sanitize(text: str, key: str) -> str:
    """错误信息中若出现完整 Key，统一替换为脱敏形式。"""
    return text.replace(key, mask_key(key)) if key else text


def format_quote_time(value: Any) -> str:
    """FMP 的时间戳可能是 Unix 秒，也可能是字符串，统一转成可读文本。"""
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    return str(value) if value not in (None, "") else "—"


def _first_quote(payload: Any) -> dict[str, Any]:
    """从 FMP 响应中取出第一条行情；错误响应统一抛出 FMPError。"""
    if isinstance(payload, dict):
        message = payload.get("Error Message") or payload.get("message") or payload.get("error")
        if message:
            raise FMPError(str(message))
        raise FMPError("返回格式异常（不是行情列表）")
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict) and item.get("symbol"):
                return item
        raise FMPError("返回空列表（Key 无权限或标的不存在）")
    raise FMPError("返回格式异常（不是行情列表）")


def verify_key(
    http: HttpClient,
    *,
    api_key: str,
    base_url: str,
    symbol: str = "AAPL",
) -> dict[str, Any]:
    """依次尝试 FMP stable 与 legacy 行情接口，任一成功即视为 Key 有效。"""
    if not api_key:
        raise FMPError("未配置 FMP_API_KEY（请在项目根目录 .env 中写入 FMP_API_KEY=...）")

    base = base_url.rstrip("/")
    candidates = [
        ("stable /quote", f"{base}/stable/quote", {"symbol": symbol}),
        ("legacy /api/v3/quote", f"{base}/api/v3/quote/{symbol}", {}),
    ]
    errors: list[str] = []
    for name, url, extra in candidates:
        started = time.time()
        try:
            payload = http.get_json(url, params={**extra, "apikey": api_key})
            quote = _first_quote(payload)
        except HttpError as exc:
            if exc.status in (401, 403):
                errors.append(f"{name}: Key 无效或无权限（HTTP {exc.status}）")
            else:
                errors.append(f"{name}: {_sanitize(str(exc), api_key)}")
            continue
        except FMPError as exc:
            errors.append(f"{name}: {_sanitize(str(exc), api_key)}")
            continue
        except Exception as exc:  # 网络错误等
            errors.append(f"{name}: {_sanitize(str(exc), api_key)}")
            continue

        return {
            "endpoint": name,
            "symbol": quote.get("symbol") or symbol,
            "price": quote.get("price"),
            "change": quote.get("change"),
            "exchange": quote.get("exchange"),
            "timestamp": format_quote_time(quote.get("timestamp")),
            "latency_ms": int((time.time() - started) * 1000),
            "masked_key": mask_key(api_key),
        }

    raise FMPError("；".join(errors))
