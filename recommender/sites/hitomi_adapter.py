# -*- coding: utf-8 -*-
"""hitomi.la 适配器：无头 Chrome 渲染站方搜索页（站内搜索为纯 JS 渲染的二进制索引链，
直接用站方自己的前端逻辑最稳）。仅用于"搜索"菜单，不参与推荐生成（较慢）。

结果卡片结构：
  <div class="manga"> <a href="/{type}/{slug}-{gallery_id}.html">…
  <h1 class="lillie"><a …>标题</a></h1>
  <div class="artist-list"><ul><li><a href="/artist/{name}-all.html">作者</a>
  <img data-src="//tn.gold-usergeneratedcontent.net/webpbigtn/…">
"""
from __future__ import annotations

import re
from urllib.parse import quote

from ..config import config
from .base import SiteAdapter, Work

_GID_RE = re.compile(r"-(\d+)\.html")
_TITLE_RE = re.compile(r'<h1 class="lillie">\s*<a[^>]*>(.*?)</a>', re.S)
_ARTIST_RE = re.compile(r'/artist/([^"<]+?)-all\.html">\s*([^<]+)</a>')
_COVER_RE = re.compile(r'data-src="(//tn\.gold-usergeneratedcontent\.net/webpbigtn/[^"]+)"')
_TAG_RE = re.compile(r'/tag/([^"]+?)-all\.html')

DATA_DOMAIN = "gold-usergeneratedcontent.net"


class HitomiAdapter(SiteAdapter):
    name = "hitomi"
    domain = "hitomi.la"

    def _probe(self) -> bool:
        from curl_cffi import requests as cr

        proxy = config.get("proxy", "")
        kwargs = {"impersonate": "chrome", "timeout": 20}
        if proxy:
            kwargs["proxies"] = {"http": proxy, "https": proxy}
        try:
            r = cr.get(f"https://ltn.{DATA_DOMAIN}/common.js", **kwargs)
            return r.status_code == 200
        except Exception as e:  # noqa: BLE001
            self.reason_unavailable = f"{type(e).__name__}: {str(e)[:60]}"
            return False

    def search_light(self, query: str, limit: int = 6, author_mode: bool = False) -> list[Work]:
        from playwright.sync_api import sync_playwright

        q = f"artist:{query}" if author_mode else query
        url = f"https://hitomi.la/search.html?{quote(q, safe='')}"
        proxy = config.get("proxy", "")
        launch = {"channel": "chrome", "headless": True}
        if proxy:
            launch["proxy"] = {"server": proxy}
        html = ""
        with sync_playwright() as p:
            browser = p.chromium.launch(**launch)
            ctx = browser.new_context(locale="zh-CN")
            page = ctx.new_page()
            page.goto(url, timeout=60000)
            for _ in range(16):  # 等站方 JS 渲染结果
                page.wait_for_timeout(1500)
                html = page.content()
                if html.count("lillie") >= 2 or "no-results" in html and html.count("hidden") == 0:
                    break
            browser.close()
        works: list[Work] = []
        for block in re.split(r'<div class="manga">', html)[1:]:
            if len(works) >= limit:
                break
            m = _GID_RE.search(block)
            if not m:
                continue
            gid = m.group(1)
            tm = _TITLE_RE.search(block)
            title = re.sub(r"<[^>]+>", "", tm.group(1)).strip() if tm else ""
            am = _ARTIST_RE.search(block)
            author = am.group(1).strip() if am else ""
            cm = _COVER_RE.search(block)
            cover = ("https:" + cm.group(1)) if cm else ""
            tags: list[str] = []
            for tm2 in _TAG_RE.finditer(block):
                tag = tm2.group(1).strip()
                if tag and tag not in tags:
                    tags.append(tag)
            works.append(
                Work(
                    site=self.name,
                    work_id=gid,
                    title=title or f"gallery/{gid}",
                    author=author,
                    tags=tags,
                    url=f"https://hitomi.la/galleries/{gid}.html",
                    is_chinese=any(t.startswith("language:chinese") for t in tags),
                    raw={"cover": cover},
                )
            )
        return works

    def search_author(self, author: str, limit: int = 12) -> list[Work]:
        return self.search_light(author, limit, author_mode=True)

    def search(self, query: str, limit: int = 12) -> list[Work]:
        return self.search_light(query, limit)
