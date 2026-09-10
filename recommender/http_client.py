# -*- coding: utf-8 -*-
"""统一 HTTP 客户端：代理、UA、按域名限速、重试与退避。"""
from __future__ import annotations

import threading
import time

import requests

from .config import config


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message[:300]}")
        self.status = status


_lock = threading.Lock()
_last_request: dict[str, float] = {}


class HttpClient:
    def __init__(self, proxy: str | None = None):
        self.session = requests.Session()
        p = proxy if proxy is not None else config.get("proxy", "")
        if p:
            self.session.proxies = {"http": p, "https": p}
        self.session.headers.update(
            {
                "User-Agent": config.get("user_agent"),
                "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
            }
        )

    def _throttle(self, domain: str, seconds: float) -> None:
        with _lock:
            now = time.monotonic()
            wait = _last_request.get(domain, 0.0) + seconds - now
            if wait > 0:
                time.sleep(wait)
            _last_request[domain] = time.monotonic()

    def _interval_for(self, domain: str) -> float:
        return float(config.get("rate_limits", {}).get(domain, 2))

    def request(self, method: str, url: str, domain: str, **kwargs) -> requests.Response:
        interval = self._interval_for(domain)
        last_err: Exception | None = None
        for attempt in range(4):
            self._throttle(domain, interval)
            try:
                r = self.session.request(method, url, timeout=25, **kwargs)
            except requests.RequestException as e:
                last_err = e
                if attempt == 3:
                    break
                time.sleep(2**attempt * 2)
                continue
            if r.status_code in (429, 509, 500, 502, 503):
                if attempt == 3:
                    raise HttpError(r.status_code, r.text)
                time.sleep(2**attempt * 5)
                continue
            if r.status_code == 403:
                raise HttpError(403, r.text)
            return r
        raise HttpError(0, str(last_err or "unreachable"))

    def get(self, url: str, domain: str, **kwargs) -> requests.Response:
        return self.request("GET", url, domain, **kwargs)

    def post_json(self, url: str, domain: str, payload: dict, **kwargs) -> requests.Response:
        return self.request("POST", url, domain, json=payload, **kwargs)


def check_reachable(url: str, domain: str, client: HttpClient | None = None) -> tuple[int, str]:
    """轻量可达性探测，返回 (status, 说明)。"""
    c = client or HttpClient()
    try:
        r = c.get(url, domain)
        return r.status_code, f"HTTP {r.status_code} ({len(r.content)} bytes)"
    except HttpError as e:
        return e.status, str(e)
    except Exception as e:  # noqa: BLE001
        return -1, str(e)
