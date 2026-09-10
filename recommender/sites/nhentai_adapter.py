# -*- coding: utf-8 -*-
"""nhentai 适配器：官方 v2 API（经 curl_cffi 伪装 Chrome 指纹访问）。

    GET /api/v2/search?query=...&page=1       搜索（结果为 id/title/num_pages/num_favorites/tag_ids）
    GET /api/v2/galleries/{id}                详情（tags 为 {type,name,slug} 对象）
    GET /api/v2/galleries/popular             热门（列表，约 5 条）
    GET /api/v2/galleries/random              随机

注意：搜索结果的标签是 tag_ids，需要逐条取详情才能拿到标签名（限速 2s）。
"""
from __future__ import annotations

import threading
import time

from curl_cffi import requests as curl_requests

from ..config import config
from .base import SiteAdapter, Work

_lock = threading.Lock()
_last_ts = 0.0


def _throttle(seconds: float) -> None:
    global _last_ts
    with _lock:
        wait = _last_ts + seconds - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_ts = time.monotonic()


class NhentaiAdapter(SiteAdapter):
    name = "nhentai"
    domain = "nhentai.net"

    def __init__(self) -> None:
        super().__init__()
        self.session = curl_requests.Session(impersonate="chrome")
        proxy = config.get("proxy", "")
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            }
        )
        self.interval = float(config.get("rate_limits", {}).get(self.domain, 2))

    def _probe(self) -> bool:
        try:
            _throttle(self.interval)
            r = self.session.get("https://nhentai.net/api/v2/galleries/popular", timeout=25)
            return r.status_code == 200
        except Exception as e:  # noqa: BLE001
            self.reason_unavailable = f"{type(e).__name__}: {str(e)[:60]}"
            return False

    def _get_json(self, url: str, params: dict | None = None):
        for attempt in range(3):
            _throttle(self.interval)
            try:
                r = self.session.get(url, params=params, timeout=25)
            except Exception:  # noqa: BLE001
                time.sleep(2**attempt)
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code == 403:
                raise RuntimeError("nhentai 403（被拦截）")
            time.sleep(2**attempt)
        raise RuntimeError("nhentai 请求失败")

    @staticmethod
    def _to_work(g: dict, tags: list[dict] | None = None) -> Work:
        gid = g["id"]
        tags = tags or []
        tag_list: list[str] = []
        author = ""
        for t in tags:
            ttype = t.get("type", "tag")
            name = t.get("name", "")
            if not name:
                continue
            if ttype == "tag":
                full = name
            else:
                full = f"{ttype}:{name}"
            if full not in tag_list:
                tag_list.append(full)
            if ttype == "artist" and not author:
                author = name
        title = g.get("japanese_title") or g.get("english_title") or ""
        if not title and isinstance(g.get("title"), dict):
            t = g["title"]
            title = t.get("japanese") or t.get("english") or ""
        thumb = g.get("thumbnail") or ""
        if isinstance(thumb, dict):
            thumb = thumb.get("path") or ""
        return Work(
            site="nhentai",
            work_id=str(gid),
            title=title,
            author=author,
            tags=tag_list,
            category="",
            rating=0.0,
            rating_count=int(g.get("num_favorites") or 0),
            pages=int(g.get("num_pages") or 0),
            url=f"https://nhentai.net/g/{gid}/",
            posted=str(g.get("upload_date") or ""),
            is_chinese=any(t.get("type") == "language" and t.get("name") == "chinese" for t in tags),
            raw={"thumbnail": thumb, "media_id": g.get("media_id", "")},
        )

    def _detail(self, gid: int) -> dict:
        return self._get_json(f"https://nhentai.net/api/v2/galleries/{gid}")

    def search_light(self, query: str, limit: int = 6, author_mode: bool = False) -> list[Work]:
        """轻量搜索：直接用搜索响应（标题/页数/收藏/缩略图），不逐条取详情。"""
        q = f'artist:"{query}"' if author_mode else query
        data = self._get_json("https://nhentai.net/api/v2/search", {"query": q, "page": 1})
        out = []
        total = int(data.get("num_pages") or 0) * int(data.get("per_page") or 0) or None
        for g in (data.get("result") or [])[:limit]:
            title = g.get("japanese_title") or g.get("english_title") or ""
            thumb = g.get("thumbnail") or {}
            p = thumb.get("path") if isinstance(thumb, dict) else thumb
            out.append(
                Work(
                    site=self.name,
                    work_id=str(g["id"]),
                    title=title,
                    url=f"https://nhentai.net/g/{g['id']}/",
                    rating_count=int(g.get("num_favorites") or 0),
                    pages=int(g.get("num_pages") or 0),
                    raw={"thumb": p or "", "total": total},
                )
            )
        return out

    def search(self, query: str, limit: int = 15) -> list[Work]:
        data = self._get_json("https://nhentai.net/api/v2/search", {"query": query, "page": 1})
        results = (data.get("result") or [])[:limit]
        works = []
        for g in results:
            try:
                detail = self._detail(g["id"])
                works.append(self._to_work(detail, detail.get("tags") or []))
            except RuntimeError:
                continue
        return works

    def search_author(self, author: str, limit: int = 15) -> list[Work]:
        return self.search(f'artist:"{author}"', limit)

    def trending(self, limit: int = 5) -> list[Work]:
        # 优先取"中文 + 按热门排序"（贴合硬性中文要求），站内语言过滤不可用时回退 popular 榜
        data = None
        try:
            data = self._get_json("https://nhentai.net/api/v2/search",
                                  {"query": "language:chinese", "sort": "popular", "page": 1})
            results = (data.get("result") or [])[:limit]
        except RuntimeError:
            results = []
        if not results:
            data = self._get_json("https://nhentai.net/api/v2/galleries/popular")
            results = (data or [])[:limit]
        works = []
        for g in results:
            try:
                detail = self._detail(g["id"])
                works.append(self._to_work(detail, detail.get("tags") or []))
            except RuntimeError:
                continue
        return works
