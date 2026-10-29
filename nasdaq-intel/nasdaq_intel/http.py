"""HTTP 客户端：统一 UA、重试、按域名限速。"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class HttpError(RuntimeError):
    pass


class HttpClient:
    def __init__(
        self,
        user_agent: str,
        timeout: int = 60,
        max_retries: int = 3,
        min_intervals: dict[str, float] | None = None,
        sec_user_agent: str | None = None,
    ) -> None:
        self.timeout = timeout
        self.min_intervals = min_intervals or {}
        self.sec_user_agent = sec_user_agent or user_agent
        self._last_request: dict[str, float] = {}

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        retry = Retry(
            total=max_retries,
            connect=max_retries,
            read=max_retries,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _throttle(self, url: str) -> None:
        host = urlparse(url).hostname or ""
        interval = 0.0
        for key, value in self.min_intervals.items():
            if key in host:
                interval = max(interval, value)
        if interval <= 0:
            return
        last = self._last_request.get(host, 0.0)
        wait = interval - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        self._last_request[host] = time.time()

    def get(self, url: str, *, params: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None) -> requests.Response:
        self._throttle(url)
        merged_headers = dict(headers or {})
        if "sec.gov" in (urlparse(url).hostname or ""):
            # SEC 声明要求使用带联系方式的 User-Agent
            merged_headers.setdefault("User-Agent", self.sec_user_agent)
        resp = self.session.get(url, params=params, headers=merged_headers, timeout=self.timeout)
        if resp.status_code >= 400:
            raise HttpError(f"GET {url} -> HTTP {resp.status_code}: {resp.text[:200]}")
        return resp

    def get_json(self, url: str, *, params: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None) -> Any:
        resp = self.get(url, params=params, headers=headers)
        try:
            return resp.json()
        except ValueError as exc:  # 返回非 JSON 时给出可读错误
            raise HttpError(f"GET {url} -> 非 JSON 响应: {resp.text[:200]}") from exc

    def get_text(self, url: str, *, params: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None) -> str:
        return self.get(url, params=params, headers=headers).text
