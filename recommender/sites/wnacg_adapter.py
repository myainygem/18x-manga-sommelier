# -*- coding: utf-8 -*-
"""wnacg（中文汉化站）适配器：列表页解析，结果全部视为中文版。"""
from __future__ import annotations

import re
from urllib.parse import quote

from ..http_client import HttpClient, HttpError
from .base import SiteAdapter, Work

_ITEM_RE = re.compile(
    r'<li class="li gallary_item">(.*?)</li>', re.S
)
_LINK_RE = re.compile(r'href="(/photos-index-aid-(\d+)\.html)"\s+title="(.*?)"', re.S)
# 注意：搜索命中时标题里会插入 <em> 高亮，img 标签内 alt 含 <em> 的 ">" 会截断
# 形如 <img[^>]+src="..." 的正则，因此直接锚定 src 属性本身。
_IMG_RE = re.compile(r'\bsrc="(//[^"]+)"', re.S)
_INFO_RE = re.compile(r'(\d+)\s*張圖片', re.S)
_DATE_RE = re.compile(r"創建於([\d\- :]+)", re.S)


class WnacgAdapter(SiteAdapter):
    name = "wnacg"
    domain = "wnacg.com"

    def __init__(self) -> None:
        super().__init__()
        self.client = HttpClient()

    def _probe(self) -> bool:
        try:
            r = self.client.get("https://www.wnacg.com/", self.domain)
            return r.status_code == 200
        except HttpError:
            return False

    def search(self, query: str, limit: int = 15) -> list[Work]:
        url = f"https://www.wnacg.com/search/?q={quote(query)}"
        r = self.client.get(url, self.domain)
        works: list[Work] = []
        for block in _ITEM_RE.findall(r.text)[:limit]:
            m = _LINK_RE.search(block)
            if not m:
                continue
            title = re.sub(r"<[^>]+>", "", m.group(3)).strip()
            pm = _INFO_RE.search(block)
            pages = int(pm.group(1)) if pm else 0
            dm = _DATE_RE.search(block)
            posted = dm.group(1).strip() if dm else ""
            im = _IMG_RE.search(block)
            cover = ("https:" + im.group(1)) if im else ""
            author = ""
            am = re.match(r"^\[([^\[\]]+)\]", title)
            if am:
                head = am.group(1)
                if "(" in head and head.endswith(")"):
                    _, _, artist = head.rpartition("(")
                    author = artist.rstrip(")").strip()
                else:
                    author = head
            works.append(
                Work(
                    site=self.name,
                    work_id=m.group(2),
                    title=title,
                    author=author,
                    tags=[],
                    category="",
                    rating=0.0,
                    rating_count=0,
                    pages=pages,
                    url=f"https://www.wnacg.com{m.group(1)}",
                    posted=posted,
                    is_chinese=True,
                    raw={"cover": cover},
                )
            )
        return works

    def search_light(self, query: str, limit: int = 6, author_mode: bool = False) -> list[Work]:
        return self.search(query, limit)

    def search_author(self, author: str, limit: int = 15) -> list[Work]:
        return self.search(author, limit)
