"""只读报告站点：把 data/reports 下的 Markdown 日报渲染为网页。

路由：
  GET /                     最新一份日报
  GET /reports/<YYYY-MM-DD> 指定日期日报
  GET /download/<date>.md   下载原始 Markdown
  GET /api/reports          日报列表 JSON
  GET /health               健康检查
"""

from __future__ import annotations

import html
import json
import os
import re
import socket
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import markdown

ROOT = Path(os.environ.get("NASDAQ_INTEL_ROOT", Path(__file__).resolve().parent.parent))
DATA_DIR = Path(os.environ.get("NASDAQ_INTEL_DATA_DIR", str(ROOT / "data")))
REPORT_DIR = DATA_DIR / "reports"


def default_host() -> str:
    """优先使用显式配置；否则绑定本机对外网卡，避免与已有回环服务抢同一端口。"""
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

CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body {
  margin: 0; background: #f4f5f7; color: #1c1f26;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
    "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  font-size: 15px; line-height: 1.75;
}
header {
  position: sticky; top: 0; z-index: 10;
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 24px; height: 56px; background: rgba(255,255,255,.92);
  backdrop-filter: blur(8px); border-bottom: 1px solid #e3e6eb;
}
.brand { display: flex; align-items: center; gap: 10px; font-weight: 650; letter-spacing: .2px; }
.brand .mark {
  width: 22px; height: 22px; border-radius: 5px; flex: none;
  background: linear-gradient(135deg, #1b3a6b, #2e7d8f);
}
.brand a { color: #1c1f26; text-decoration: none; }
header .meta { color: #7a8190; font-size: 12.5px; }
.layout {
  display: grid; grid-template-columns: 232px minmax(0, 1fr);
  gap: 28px; max-width: 1120px; margin: 28px auto 56px; padding: 0 24px;
  align-items: start;
}
@media (max-width: 820px) {
  .layout { grid-template-columns: 1fr; gap: 16px; margin-top: 18px; }
  aside { position: static !important; }
}
aside {
  position: sticky; top: 84px; background: #fff; border: 1px solid #e3e6eb;
  border-radius: 10px; padding: 16px 14px; max-height: calc(100vh - 110px);
  overflow: auto;
}
aside h2 { font-size: 12px; letter-spacing: .12em; text-transform: uppercase; color: #8a91a0; margin: 0 0 10px 6px; }
aside ol { list-style: none; margin: 0; padding: 0; }
aside li a {
  display: flex; justify-content: space-between; gap: 8px;
  padding: 7px 8px; border-radius: 7px; text-decoration: none;
  color: #3a4150; font-size: 13.5px;
}
aside li a:hover { background: #f2f4f7; }
aside li a.active { background: #eef3fb; color: #144e9e; font-weight: 600; }
aside li a span.day { color: #98a0af; font-size: 12px; }
main {
  background: #fff; border: 1px solid #e3e6eb; border-radius: 12px;
  padding: 36px 44px 32px; min-width: 0;
  box-shadow: 0 1px 2px rgba(20,25,35,.04);
}
@media (max-width: 820px) { main { padding: 24px 18px; } }
.doc h1 { font-size: 26px; line-height: 1.35; margin: 0 0 18px; letter-spacing: -.01em; }
.doc h2 {
  font-size: 18px; margin: 32px 0 12px; padding-left: 10px;
  border-left: 3px solid #2e7d8f; line-height: 1.4;
}
.doc h3 { font-size: 16px; margin: 24px 0 8px; }
.doc p { margin: 10px 0; }
.doc ul { margin: 8px 0 8px 22px; padding: 0; }
.doc li { margin: 3px 0; }
.doc a { color: #1258b8; text-decoration: none; border-bottom: 1px solid rgba(18,88,184,.25); }
.doc a:hover { border-bottom-color: #1258b8; }
.doc blockquote {
  margin: 12px 0; padding: 10px 14px; color: #5b6272;
  background: #f7f8fa; border-left: 3px solid #d8dde5; border-radius: 0 6px 6px 0;
}
.doc table { border-collapse: collapse; width: 100%; margin: 14px 0; font-size: 13.5px; }
.doc th, .doc td { border: 1px solid #e3e6eb; padding: 8px 10px; text-align: left; }
.doc th { background: #f4f6f9; font-weight: 600; color: #3a4150; }
.doc tr:nth-child(even) td { background: #fbfcfd; }
.doc hr { border: 0; border-top: 1px solid #e3e6eb; margin: 28px 0 14px; }
.doc em { color: #6b7280; }
footer { text-align: center; color: #98a0af; font-size: 12.5px; padding: 0 24px 40px; }
.empty { padding: 60px 0; text-align: center; color: #7a8190; }
.empty code { background: #f2f4f7; padding: 3px 6px; border-radius: 4px; color: #3a4150; }
"""


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


def render_page(day: date | None, markdown_text: str | None, reports: list[date]) -> str:
    nav_items = []
    for item in reports[:120]:
        active = " class=\"active\"" if day == item else ""
        nav_items.append(
            f'<li><a href="/reports/{item.isoformat()}"{active}>'
            f'{item.isoformat()}<span class="day">{WEEKDAYS[item.weekday()]}</span></a></li>'
        )
    nav = "<ol>" + "".join(nav_items) + "</ol>" if nav_items else '<p class="meta">暂无归档</p>'

    if markdown_text is None:
        body = (
            '<div class="empty"><p>暂无日报。</p>'
            "<p>在服务器执行 <code>python3 run.py run --no-llm</code> 生成第一份日报。</p></div>"
        )
        title = "纳斯达克科技与资本情报日报"
    else:
        body = markdown.Markdown(extensions=["tables"]).convert(markdown_text)
        title = f"纳斯达克科技与资本情报日报 · {day.isoformat()}"

    updated = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <div class="brand"><span class="mark"></span><a href="/">纳斯达克科技与资本情报</a></div>
  <div class="meta">数据源：Nasdaq · SEC EDGAR · PR Newswire</div>
</header>
<div class="layout">
  <aside><h2>日报归档</h2>{nav}</aside>
  <main><article class="doc">{body}</article></main>
</div>
<footer>服务时间 {updated}（服务器本地时间）· 页面内容由确定性模板生成，不构成投资建议</footer>
</body>
</html>"""


def error_page(code: int, message: str) -> str:
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{code}</title><style>{CSS}</style></head>
<body><header><div class="brand"><span class="mark"></span>
<a href="/">纳斯达克科技与资本情报</a></div></header>
<main style="max-width:720px;margin:40px auto;"><div class="empty"><h1>{code}</h1>
<p>{html.escape(message)}</p><p><a href="/">返回最新日报</a></p></div></main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "NasdaqIntel/0.1"

    def do_GET(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/health":
            self._send("ok".encode(), "text/plain; charset=utf-8")
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

        forward = self.headers.get("X-Forwarded-Prefix", "")

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

    def _send_html(self, body: str, status: int = 200) -> None:
        self._send(body.encode("utf-8"), "text/html; charset=utf-8", status)

    def _send(self, body: bytes, content_type: str, status: int = 200, extra_headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"serving reports from {REPORT_DIR} on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
