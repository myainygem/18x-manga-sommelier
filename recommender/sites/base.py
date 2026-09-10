# -*- coding: utf-8 -*-
"""站点适配器公共类型。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Work:
    site: str
    work_id: str
    title: str
    author: str = ""
    tags: list = field(default_factory=list)
    category: str = ""
    rating: float = 0.0
    rating_count: int = 0
    pages: int = 0
    url: str = ""
    posted: str = ""
    is_chinese: bool = False
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "site": self.site, "work_id": self.work_id, "title": self.title,
            "author": self.author, "tags": self.tags, "category": self.category,
            "rating": self.rating, "rating_count": self.rating_count,
            "pages": self.pages, "url": self.url, "posted": self.posted,
            "is_chinese": self.is_chinese,
        }


class SiteAdapter:
    name = "base"
    domain = ""
    reason_unavailable = ""

    def __init__(self) -> None:
        self._avail: bool | None = None

    def available(self) -> bool:
        """自动探测可用性（缓存结果）。不可用原因写入 reason_unavailable。"""
        if self._avail is not None:
            return self._avail
        self._avail = self._probe()
        return self._avail

    def _probe(self) -> bool:
        return True

    def search(self, query: str, limit: int = 15) -> list[Work]:
        raise NotImplementedError

    def search_author(self, author: str, limit: int = 15) -> list[Work]:
        return self.search(author, limit)

    def trending(self, limit: int = 15) -> list[Work]:
        return []
