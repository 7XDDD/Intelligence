"""NASDAQ 科技与资本情报日报 — 可视化渲染版

在原有 Markdown 只读站点基础上，新增：
  - /assets/<name>  静态资源（doc.css / doc.js / echarts.min.js）
  - render_page()  新渲染层：解析 Markdown → KPI + ECharts + 表格 + Callout

保留所有原有路由：/  /reports/<date>  /download/<date>.md  /api/reports  /health
"""

from __future__ import annotations

import html
import hmac
import json
import os
import re
import socket
from base64 import b64decode
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import markdown

ROOT = Path(os.environ.get("NASDAQ_INTEL_ROOT", Path(__file__).resolve().parent.parent))
DATA_DIR = Path(os.environ.get("NASDAQ_INTEL_DATA_DIR", str(ROOT / "data")))
REPORT_DIR = DATA_DIR / "reports"
ASSET_DIR = Path(__file__).resolve().parent / "assets"
WEB_USER = os.environ.get("NASDAQ_INTEL_WEB_USER", "").strip()
WEB_PASSWORD = os.environ.get("NASDAQ_INTEL_WEB_PASSWORD", "").strip()


def default_host() -> str:
    explicit = os.environ.get("NASDAQ_INTEL_HOST")
    if explicit:
        return explicit
    try:
        candidates = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        candidates = []
    for address in candidates:
        if not address.startswith("127."):
            return address
    return "0.0.0.0"


HOST = default_host()
PORT = int(os.environ.get("NASDAQ_INTEL_PORT", "8040"))

DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# ============================================================
# 原有工具函数（保持不变）
# ============================================================

def list_reports() -> list[date]:
    if not REPORT_DIR.exists():
        return []
    found: list[date] = []
    for path in REPORT_DIR.glob("*/*/*.md"):
        if DATE_RE.match(path.stem):
            try:
                found.append(date.fromisoformat(path.stem))
            except ValueError:
                continue
    return sorted(found, reverse=True)


def report_path(day: date) -> Path:
    return REPORT_DIR / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.isoformat()}.md"


def error_page(code: int, message: str) -> str:
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{code}</title>
<style>body{{font-family:-apple-system,"Segoe UI",sans-serif;margin:40px auto;max-width:720px;background:#fff;color:#1c1f26}}a{{color:#1258b8}}</style>
</head>
<body>
<h1>{code}</h1><p>{html.escape(message)}</p><p><a href="/">返回最新日报</a></p>
</body></html>"""


# ============================================================
# Markdown → 结构化数据解析器（新增）
# ============================================================

def _section(md: str, title: str) -> str:
    """抽取 ## 标题下的段落直到下一个 ## 或文件末尾。"""
    pattern = re.compile(rf"^##\s+{re.escape(title)}\s*$([\s\S]*?)(?=^##\s+|\Z)", re.MULTILINE)
    m = pattern.search(md)
    return m.group(1).strip() if m else ""


def parse_stock_table(md: str) -> list[dict]:
    """从 ## 四、市场表现 下的 Markdown 表格抽取行情数据。"""
    block = _section(md, "四、市场表现")
    if not block:
        return []
    rows = []
    # 匹配 markdown 表格数据行（忽略 header 和分隔线）
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 8:
            continue
        if cells[0] in ("公司", "---"):
            continue
        company, symbol, price, prev, close, change_raw, ts, state = cells[:8]
        # 解析 change_raw: "+0.92（+0.33%）*" 或 "-13.54（-4.00%）*"
        pct_match = re.search(r"([+-]?\d+\.\d+)%", change_raw)
        abs_match = re.search(r"([+-]?\d+\.\d+)", change_raw)
        change_pct = float(pct_match.group(1)) if pct_match else 0.0
        change_abs = float(abs_match.group(1)) if abs_match else 0.0
        price_val = float(re.sub(r"[^\d.]", "", price)) if re.search(r"\d", price) else 0.0
        prev_val = float(re.sub(r"[^\d.]", "", prev)) if re.search(r"\d", prev) else 0.0
        rows.append({
            "symbol": symbol,
            "company": company,
            "price": price_val,
            "prev": prev_val,
            "change": change_abs,
            "change_pct": change_pct,
            "close": close,
            "time": ts,
            "state": state,
        })
    return rows


def parse_event_category(md: str) -> list[dict]:
    """从 ## 三、行业趋势与竞争格局 下的 bullet 行 抽取事件分类。
    形如："事件分布：其他 57 条，财务 16 条，商业 4 条，资本 3 条，技术 3 条（共 83 个归并事件）。"
    """
    block = _section(md, "三、行业趋势与竞争格局")
    if not block:
        return []
    results = []
    for line in block.splitlines():
        line = line.strip()
        if "事件分布" not in line:
            continue
        # 匹配 "类别 N 条" 模式
        for cat, num in re.findall(r"([^\d,，（]+)\s+(\d+)\s*条", line):
            cat = cat.strip().strip("，,（(")
            if cat:
                results.append({"category": cat, "count": int(num)})
    return results


def parse_event_by_company(md: str) -> list[dict]:
    """从 ## 三 中的 "信息最集中的公司：GOOGL（19 条）..." 或 ## 二 中的各公司段落抽取事件量。
    优先用 ## 三 的汇总行，若缺失则从 ## 二 的 "今日重要事件" 行解析。
    """
    block3 = _section(md, "三、行业趋势与竞争格局")
    results = []
    if block3:
        for line in block3.splitlines():
            m = re.search(r"信息最集中的公司[：:]\s*(.+)", line)
            if m:
                for sym, num in re.findall(r"([A-Z]{2,6})[^\d]*(\d+)\s*条", m.group(1)):
                    results.append({"symbol": sym, "count": int(num)})
    if results:
        return results
    # fallback: 从 ## 二 逐公司解析
    block2 = _section(md, "二、公司动态")
    if not block2:
        return []
    sym_counter: dict[str, int] = {}
    for line in block2.splitlines():
        m = re.match(r"###\s+.*\(([A-Z]{2,6})\)", line)
        if m:
            sym = m.group(1)
            sm = re.search(r"今日重要事件[：:]\s*S/A\s*(\d+)\s*条.*B\s*(\d+)\s*条", line + "\n" + _peek_next_lines(block2, line))
            if sm:
                sym_counter[sym] = int(sm.group(1)) + int(sm.group(2))
            else:
                sym_counter[sym] = 0
    return [{"symbol": s, "count": c} for s, c in sym_counter.items()]


def _peek_next_lines(text: str, current_line: str) -> str:
    """在 text 中找 current_line 之后的 5 行（用来匹配跨行的 "今日重要事件" 行）。"""
    lines = text.splitlines()
    try:
        idx = lines.index(current_line)
    except ValueError:
        return ""
    return "\n".join(lines[idx:idx + 8])


def parse_top_events(md: str) -> list[dict]:
    """从 ## 一、今日核心情报 抽取 A/B 级事件。"""
    block = _section(md, "一、今日核心情报")
    results = []
    # 每个事件以 "### N. [标题](url)" 开始
    for ev in re.split(r"\n###\s+", block):
        ev = ev.strip()
        if not ev:
            continue
        first_line = ev.split("\n")[0]
        title_match = re.search(r"\[(.+?)\]\((.+?)\)", first_line)
        level_match = re.search(r"等级[：:]\s*([A-Z])", ev)
        company_match = re.search(r"公司[：:]\s*([^|｜]+?)\s*[|｜]", ev)
        if title_match:
            results.append({
                "title": title_match.group(1),
                "url": title_match.group(2),
                "level": level_match.group(1) if level_match else "B",
                "company": company_match.group(1).strip() if company_match else "",
            })
    return results


def parse_kpis(md: str) -> dict:
    """从 Markdown 头部的 bullet 元信息 + 事件总量行抽取 KPI 数据。"""
    info = {}
    # 事件总数（文末）
    m = re.search(r"事件总数[：:]\s*(\d+)", md)
    if m:
        info["total_events"] = int(m.group(1))
    # 元信息 bullet
    if "覆盖公司" in md:
        m = re.search(r"覆盖公司[：:]\s*(\d+)/(\d+)", md)
        if m:
            info["companies"] = f"{m.group(1)}/{m.group(2)}"
    # 待核验
    m = re.search(r"待核验事件[：:]\s*(\d+)\s*条", md)
    if m:
        info["pending"] = int(m.group(1))
    # 归并比例
    m = re.search(r"归并比例[：:]\s*(\d+)%", md)
    if m:
        info["merge_rate"] = f"{m.group(1)}%"
    else:
        # fallback 从 "N 个多来源归并事件（归并比例 X%）"
        m2 = re.search(r"归并比例\s*(\d+)%", md)
        if m2:
            info["merge_rate"] = f"{m2.group(1)}%"
    return info


def parse_trends(md: str) -> list[str]:
    """从 ## 三 抽取 技术趋势 / 竞争格局 / 资本信号 的要点 bullet。"""
    block = _section(md, "三、行业趋势与竞争格局")
    if not block:
        return []
    bullets = []
    for line in block.splitlines():
        line = line.strip()
        if line.startswith("- ") and not re.search(r"事件分布|信息最集中|跨公司归并", line):
            bullets.append(line[2:].strip())
    return bullets[:6]


# ============================================================
# 新版渲染层：doc.css + ECharts + Callout
# ============================================================

def render_page(day: date | None, markdown_text: str | None, reports: list[date]) -> str:
    nav_items = []
    for item in reports[:120]:
        active = ' class="active"' if day == item else ""
        nav_items.append(
            f'<li><a href="/reports/{item.isoformat()}"{active}>'
            f"{item.isoformat()}<span class=\"day\">{WEEKDAYS[item.weekday()]}</span></a></li>"
        )
    nav = "<ol>" + "".join(nav_items) + "</ol>" if nav_items else '<p class="meta">暂无归档</p>'

    if markdown_text is None:
        body = (
            '<div class="empty"><p>暂无日报。</p>'
            "<p>在服务器执行 <code>python3 run.py run --no-llm</code> 生成第一份日报。</p></div>"
        )
        title = "纳斯达克科技与资本情报日报"
    else:
        title = f"Nasdaq Top 10 科技与资本情报日报 · {day.isoformat()}"
        body = _build_visual_body(day, markdown_text)

    updated = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<link rel="stylesheet" href="/assets/doc.css">
<style>
/* 顶栏样式（保留原有标识） */
.doc-page {{ padding-top: 108px; }}
.site-header {{
  position: fixed; top: 0; left: 0; right: 0; z-index: 50;
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 24px; height: 56px;
  background: rgba(255,255,255,.94); backdrop-filter: blur(8px);
  border-bottom: 1px solid #e3e6eb;
}}
.site-header .brand {{ display: flex; align-items: center; gap: 10px; font-weight: 650; letter-spacing: .2px; }}
.site-header .brand .mark {{
  width: 22px; height: 22px; border-radius: 5px; flex: none;
  background: linear-gradient(135deg, #1b3a6b, #2e7d8f);
}}
.site-header .brand a {{ color: #1c1f26; text-decoration: none; }}
.site-header .meta {{ color: #7a8190; font-size: 12.5px; }}
/* 归档侧栏 */
.nav-side {{
  position: fixed; left: 28px; top: 84px; bottom: 40px; width: 200px; z-index: 9;
  background: #fff; border: 1px solid #e3e6eb; border-radius: 10px;
  padding: 14px 10px; overflow: auto;
}}
.nav-side h2 {{ font-size: 11px; letter-spacing: .12em; text-transform: uppercase; color: #8a91a0; margin: 0 0 8px 6px; }}
.nav-side ol {{ list-style: none; margin: 0; padding: 0; }}
.nav-side li a {{
  display: flex; justify-content: space-between; gap: 6px;
  padding: 5px 8px; border-radius: 6px; text-decoration: none;
  color: #3a4150; font-size: 13px;
}}
.nav-side li a:hover {{ background: #f2f4f7; }}
.nav-side li a.active {{ background: #eef3fb; color: #144e9e; font-weight: 600; }}
.nav-side li a span.day {{ color: #98a0af; font-size: 12px; }}
@media (max-width: 1100px) {{ .nav-side {{ display: none; }} .toc-rail {{ display: none; }} }}
.doc-table {{ margin-top: 24px; }}
.doc-sources {{ margin-top: 48px; }}
.doc-sources ol {{ padding-left: 22px; color: #535a66; font-size: 13.5px; }}
.doc-sources li {{ margin: 4px 0; }}
.report-footer {{
  text-align: center; color: #98a0af; font-size: 12.5px;
  padding: 0 24px 40px; margin-top: 32px;
}}
/* 降级：Markdown 原始段落也能看 */
.raw-md p {{ margin: 10px 0; }}
.raw-md h1 {{ font-size: 22px; margin: 24px 0 10px; }}
.raw-md h2 {{ font-size: 18px; margin: 22px 0 8px; }}
.raw-md ul {{ margin: 8px 0 8px 22px; }}
.raw-md li {{ margin: 3px 0; }}
.raw-md a {{ color: #1258b8; text-decoration: none; border-bottom: 1px solid rgba(18,88,184,.25); }}
.raw-md hr {{ border: 0; border-top: 1px solid #e3e6eb; margin: 24px 0; }}
</style>
</head>
<body>

<header class="site-header">
  <div class="brand"><span class="mark"></span><a href="/">纳斯达克科技与资本情报</a></div>
  <div class="meta">数据源：Nasdaq · SEC EDGAR · PR Newswire</div>
</header>

<div class="nav-side"><h2>日报归档</h2>{nav}</div>

<main class="doc-page">{body}</main>

<footer class="report-footer">服务时间 {updated}（服务器本地时间）· 页面内容由确定性模板生成，不构成投资建议</footer>

<script src="/assets/echarts.min.js"></script>
<script src="/assets/doc.js"></script>

</body>
</html>"""


def _j(data) -> str:
    """把 Python 数据转成紧凑 JSON 字符串（给 inline script 用）。"""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _build_visual_body(day: date, md: str) -> str:
    """把 Markdown 报告转为可视化版式 HTML。"""
    kpis = parse_kpis(md)
    stocks = parse_stock_table(md)
    categories = parse_event_category(md)
    by_company = parse_event_by_company(md)
    top_events = parse_top_events(md)
    trends = parse_trends(md)

    # ======== 1. KPI Strip ========
    total = kpis.get("total_events", "—")
    companies = kpis.get("companies", "10/10")
    pending = kpis.get("pending", "—")
    merge_rate = kpis.get("merge_rate", "—")
    kpi_html = f"""
  <div class="doc-metrics">
    <div class="doc-metric">
      <span>事件总数</span><strong>{total}</strong>
      <small>其中多来源归并事件占比 {merge_rate}</small>
    </div>
    <div class="doc-metric">
      <span>覆盖公司</span><strong>{companies}</strong>
      <small>Nasdaq Top 10</small>
    </div>
    <div class="doc-metric">
      <span>待核验事件</span><strong>{pending}</strong>
      <small>单一来源或缺少正文</small>
    </div>
    <div class="doc-metric">
      <span>报告日期</span><strong>{day.isoformat()}</strong>
      <small>{WEEKDAYS[day.weekday()]} · 美东交易日</small>
    </div>
  </div>
"""

    # ======== 2. 核心情报 Callout ========
    thesis_html = ""
    a_events = [e for e in top_events if e.get("level") == "A"]
    if a_events:
        ev = a_events[0]
        thesis_html = f"""
  <section class="doc-callout">
    <p><strong>今日核心情报（{ev["level"]} 级）：</strong>{html.escape(ev["title"])}（{html.escape(ev["company"])}）。<a href="{html.escape(ev["url"])}" target="_blank">来源</a></p>
  </section>
"""

    # ======== 3. 行情 Chart ========
    stock_chart = ""
    if stocks:
        stock_json = _j(stocks)
        stock_chart = f"""
  <section id="market" class="doc-chart">
    <h2>一、今日市场表现（盘前快照）</h2>
    <p>以下为 {day.isoformat()} 美东时间 9:10 AM 的盘前行情快照，带 * 的涨跌 = 盘中价 − 前收。</p>
    <figure class="chart-frame">
      <div class="chart-canvas" data-chart="stock-gain" data-data="data-stock-gain" data-type="bar"></div>
    </figure>
    <script type="application/json" id="data-stock-gain">{stock_json}</script>
    <p class="figure-caption"><strong>图 1 · Top 10 科技股盘前涨跌幅</strong><span>单位：%。蓝色上涨，玫红色下跌，按涨幅递增排序。数据源：api.nasdaq.com。</span></p>
  </section>
"""

    # ======== 4. 行情 Table ========
    table_html = ""
    if stocks:
        rows_html = []
        for s in stocks:
            cls_pct = ""
            sign = "+" if s["change_pct"] >= 0 else ""
            rows_html.append(
                f"<tr><td>{html.escape(s['company'])}</td><td>{s['symbol']}</td>"
                f"<td class=\"num\">${s['price']:.2f}</td>"
                f"<td class=\"num\">${s['prev']:.2f}</td>"
                f"<td class=\"num\">{sign}{s['change']:.2f}</td>"
                f"<td class=\"num\">{sign}{s['change_pct']:.2f}%</td>"
                f"<td>{html.escape(s['time'])}</td>"
                f"<td>{html.escape(s['state'])}</td></tr>"
            )
        table_html = f"""
  <section class="doc-table">
    <h2>完整行情快照</h2>
    <div class="table-scroll">
      <table class="doc-data-table">
        <thead><tr><th>公司</th><th>代码</th><th class="num">最新价</th><th class="num">前收</th><th class="num">日涨跌</th><th class="num">涨跌幅</th><th>快照时间（ET）</th><th>状态</th></tr></thead>
        <tbody>{''.join(rows_html)}</tbody>
      </table>
    </div>
  </section>
"""

    # ======== 5. 事件分类 Chart ========
    cat_chart = ""
    if categories:
        cat_json = _j(categories)
        cat_chart = f"""
  <section id="events" class="doc-chart">
    <h2>二、事件分布概览</h2>
    <p>本次采集归并出 <strong>{total}</strong> 个事件，按分类和公司维度聚合如下。</p>
    <figure class="chart-frame">
      <div class="chart-menu" role="group">
        <button class="chart-menu-trigger" type="button" aria-label="切换图表类型">
          <span></span><span></span><span></span>
        </button>
        <div class="chart-menu-popover" role="menu">
          <button type="button" data-chart-type="pie">饼图</button>
          <button type="button" data-chart-type="bar">条形图</button>
        </div>
      </div>
      <div class="chart-canvas" data-chart="event-category" data-data="data-event-category" data-type="pie"></div>
    </figure>
    <script type="application/json" id="data-event-category">{cat_json}</script>
    <p class="figure-caption"><strong>图 2 · 事件类型分布</strong><span>"其他" 含个股分析、投资者评论、ETF 动态；"财务" 含财报与业绩；"商业" 含合作；"资本" 含融资发债；"技术" 含产品与研发。</span></p>
  </section>
"""

    # ======== 6. 公司事件量 Chart ========
    co_chart = ""
    if by_company:
        co_json = _j(by_company)
        co_chart = f"""
  <section class="doc-chart">
    <h2>各公司事件量排名</h2>
    <figure class="chart-frame">
      <div class="chart-canvas" data-chart="event-by-company" data-data="data-event-by-company" data-type="bar"></div>
    </figure>
    <script type="application/json" id="data-event-by-company">{co_json}</script>
    <p class="figure-caption"><strong>图 3 · 各公司事件量排名</strong><span>信息最集中的三家：{_company_top3(by_company)}。0 条事件的公司通常处于财报静默期。</span></p>
  </section>
"""

    # ======== 7. 行业趋势 Callouts ========
    trends_html = ""
    if trends:
        trend_cards = []
        labels = ["技术趋势", "竞争格局", "资本信号"]
        trend_chunks = [trends[i:i + 2] for i in range(0, min(len(trends), 6), 2)]
        for idx, chunk in enumerate(trend_chunks):
            label = labels[idx] if idx < len(labels) else "关键信号"
            items = "".join(f"<li>{html.escape(t)}</li>" for t in chunk)
            trend_cards.append(
                f'<section class="doc-callout"><p><strong>{label}：</strong></p><ul>{items}</ul></section>'
            )
        trends_html = f"""
  <section class="doc-block" id="trends">
    <h2>三、行业趋势与竞争格局</h2>
    <p>从 {total} 条事件中归纳出的结构性信号。</p>
    {''.join(trend_cards)}
  </section>
"""

    # ======== 8. 原始 Markdown 渲染（兜底 + 后续观察） ========
    # 只渲染 ## 五、后续观察清单 和 ## 六、数据质量 + 章节标题保留
    raw_section = ""
    for sec_title in ["五、后续观察清单", "六、数据质量与运行说明"]:
        content = _section(md, sec_title)
        if content:
            sec_html = markdown.Markdown(extensions=["tables"]).convert(content)
            raw_section += f'<section class="doc-block"><h2>{sec_title}</h2><div class="raw-md">{sec_html}</div></section>\n'

    return kpi_html + thesis_html + stock_chart + table_html + cat_chart + co_chart + trends_html + raw_section


def _company_top3(rows: list[dict]) -> str:
    top = sorted(rows, key=lambda r: r["count"], reverse=True)[:3]
    return " / ".join(f"{r['symbol']} {r['count']}" for r in top)


# ============================================================
# Handler — 保留所有原有路由 + 新增 /assets/
# ============================================================

class Handler(BaseHTTPRequestHandler):
    server_version = "NasdaqIntel/2.0-visual"

    def do_GET(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"

        if path == "/health":
            self._send(b"ok", "text/plain; charset=utf-8")
            return

        if path.startswith("/assets/"):
            self._serve_asset(path)
            return

        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="nasdaq-intel"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/api/reports":
            payload = {
                "reports": [
                    {"date": d.isoformat(), "url": f"/reports/{d.isoformat()}",
                     "download": f"/download/{d.isoformat()}.md"}
                    for d in list_reports()
                ]
            }
            self._send(json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return

        if path == "/":
            reports = list_reports()
            latest = reports[0] if reports else None
            text = report_path(latest).read_text(encoding="utf-8") if latest else None
            self._send_html(render_page(latest, text, reports))
            return

        match = re.match(r"^/reports/([0-9]{4}-[0-9]{2}-[0-9]{2})$", path)
        if match:
            try:
                day = date.fromisoformat(match.group(1))
            except ValueError:
                self._send_html(error_page(400, "日期格式不正确"), 400)
                return
            file = report_path(day)
            if not file.exists():
                self._send_html(error_page(404, f"没有 {day.isoformat()} 的日报"), 404)
                return
            self._send_html(render_page(day, file.read_text(encoding="utf-8"), list_reports()))
            return

        match = re.match(r"^/download/([0-9]{4}-[0-9]{2}-[0-9]{2})\.md$", path)
        if match:
            file = report_path(date.fromisoformat(match.group(1)))
            if not file.exists():
                self._send_html(error_page(404, "文件不存在"), 404)
                return
            self._send(
                file.read_bytes(),
                "text/markdown; charset=utf-8",
                extra_headers={"Content-Disposition": f'attachment; filename="{file.name}"'},
            )
            return

        self._send_html(error_page(404, f"页面不存在：{path}"), 404)

    def _serve_asset(self, path: str) -> None:
        name = path[len("/assets/"):]
        # 安全：拒绝包含 .. 或 / 或 \ 的名字
        if ".." in name or "/" in name or "\\" in name:
            self._send_html(error_page(400, "非法路径"), 400)
            return
        target = ASSET_DIR / name
        if not target.exists() or not target.is_file():
            self._send_html(error_page(404, f"资源不存在：{name}"), 404)
            return
        ext = target.suffix.lower()
        mime = {
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".ico": "image/x-icon",
            ".woff": "font/woff",
            ".woff2": "font/woff2",
        }.get(ext, "application/octet-stream")
        self._send(target.read_bytes(), mime)

    def _authorized(self) -> bool:
        if not WEB_USER or not WEB_PASSWORD:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = b64decode(header[6:]).decode("utf-8")
            user, _, password = decoded.partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(user, WEB_USER) and hmac.compare_digest(password, WEB_PASSWORD)

    def _send_html(self, body: str, status: int = 200) -> None:
        self._send(body.encode("utf-8"), "text/html; charset=utf-8", status)

    def _send(self, body: bytes, content_type: str, status: int = 200, extra_headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"serving reports from {REPORT_DIR} on http://{HOST}:{PORT}", flush=True)
    print(f"visual assets from {ASSET_DIR}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
