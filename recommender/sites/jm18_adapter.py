# -*- coding: utf-8 -*-
"""18comic（禁漫）适配器：基于开源库 jmcomic（JMComic-Crawler-Python）的 APP API 客户端。

库自行维护可用 API 域名并处理前端挑战，无需浏览器/手动验证。
代理通过环境变量传给库（jmcomic 底层 requests 尊重 HTTP(S)_PROXY）。
"""
from __future__ import annotations

import os
import threading
import time

from ..config import config
from .base import SiteAdapter, Work

_lock = threading.Lock()
_last_ts = 0.0

# 18comic 中文标签 → 画像（EH 风格）标签映射，用于打分与雷点降权
_TAG_MAP = {
    "巨乳": "female:big breasts",
    "熟女": "female:milf",
    "人妻": "female:milf",
    "口交": "female:blowjob",
    "中出": "female:nakadashi",
    "無修正": "other:uncensored",
    "无修正": "other:uncensored",
    "單行本": "other:tankoubon",
    "单行本": "other:tankoubon",
    "眼鏡": "female:glasses",
    "眼镜": "female:glasses",
    "黑皮": "female:dark skin",
    "NTR": "female:netorare",
    "全彩": "other:full color",
    "雙馬尾": "female:twintails",
    "双马尾": "female:twintails",
    "乳交": "female:paizuri",
    "校服": "female:schoolgirl uniform",
    "絲襪": "female:stockings",
    "丝袜": "female:stockings",
    "連褲襪": "female:pantyhose",
    "连裤袜": "female:pantyhose",
    "肛交": "female:anal",
    "群交": "mixed:group",
    "多人運動": "mixed:group",
    "多人运动": "mixed:group",
    "後宮": "female:harem",
    "后宫": "female:harem",
    "偷情": "female:cheating",
    "足交": "female:footjob",
    "手淫": "male:masturbation",
    "亂倫": "mixed:incest",
    "乱伦": "mixed:incest",
    "強姦": "male:rape",
    "强奸": "male:rape",
    "顏射": "male:facial",
    "颜射": "male:facial",
}


def _throttle(seconds: float) -> None:
    global _last_ts
    with _lock:
        wait = _last_ts + seconds - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_ts = time.monotonic()


class JM18Adapter(SiteAdapter):
    name = "18comic"
    domain = "18comic.vip"

    def __init__(self) -> None:
        super().__init__()
        self._client = None

    def _ensure(self):
        if self._client is not None:
            return self._client
        # 代理：jmcomic 库自身不读我们的 config，通过环境变量传递
        proxy = config.get("proxy", "")
        if proxy:
            os.environ.setdefault("HTTP_PROXY", proxy)
            os.environ.setdefault("HTTPS_PROXY", proxy)
        import jmcomic

        option = jmcomic.JmOption.default()
        # 关闭库的日志刷屏（保留错误）
        try:
            jmcomic.disable_jm_log()
        except Exception:  # noqa: BLE001
            pass
        self._client = option.new_jm_client(impl="api")
        return self._client

    def _probe(self) -> bool:
        try:
            client = self._ensure()
            page = client.search_site("a", page=1)
            return page is not None
        except Exception as e:  # noqa: BLE001
            self.reason_unavailable = f"{type(e).__name__}: {str(e)[:80]}"
            return False

    @staticmethod
    def _clean_tags(tags) -> list[str]:
        out = []
        for t in tags or []:
            if isinstance(t, str):
                name = t
            elif isinstance(t, dict):
                name = t.get("name") or t.get("title") or ""
            else:
                name = str(t)
            name = name.strip()
            if name and name not in out:
                out.append(name)
        return out

    def search_light(self, query: str, limit: int = 6, author_mode: bool = False) -> list[Work]:
        """轻量搜索：只取搜索结果（标题/作者/封面），不取详情。"""
        client = self._ensure()
        _throttle(1.0)
        page = client.search_site(query, page=1)
        out = []
        for item in page[:limit]:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            aid, info = item
            info = info or {}
            out.append(
                Work(
                    site=self.name,
                    work_id=str(aid),
                    title=(info.get("name") or "").strip() or f"album/{aid}",
                    author=(info.get("author") or "").strip(),
                    url=f"https://18comic.vip/album/{aid}/",
                    raw={"cover": f"https://cdn-msp2.18comic.vip/media/albums/{aid}_3x4.jpg"},
                )
            )
        return out

    def search(self, query: str, limit: int = 12) -> list[Work]:
        client = self._ensure()
        _throttle(1.0)
        page = client.search_site(query, page=1)
        works: list[Work] = []
        for item in page[:limit]:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            aid, info = item
            info = info or {}
            title = (info.get("name") or "").strip()
            author = (info.get("author") or "").strip()
            cat = (info.get("category") or {}).get("title", "") if isinstance(info.get("category"), dict) else ""
            work = Work(
                site=self.name,
                work_id=str(aid),
                title=title or f"album/{aid}",
                author=author,
                tags=[],
                category=cat,
                rating=0.0,
                rating_count=0,
                pages=0,
                url=f"https://18comic.vip/album/{aid}/",
                posted=str(info.get("update_at") or ""),
                is_chinese=True,
                # 禁漫 CDN 封面（msp2 子域经实测通用）
                raw={"cover": f"https://cdn-msp2.18comic.vip/media/albums/{aid}_3x4.jpg", "aid": aid},
            )
            # 详情补充：标签/点赞/页数（失败不影响主流程）
            try:
                _throttle(1.0)
                d = client.get_album_detail(aid)
                work.rating_count = int(getattr(d, "likes", 0) or 0)
                work.pages = int(getattr(d, "page_count", 0) or 0)
                work.tags = self._clean_tags(getattr(d, "tags", None))
                # 映射为画像标签，参与打分与降权
                for t in list(work.tags):
                    mapped = _TAG_MAP.get(t)
                    if mapped and mapped not in work.tags:
                        work.tags.append(mapped)
            except Exception:  # noqa: BLE001
                pass
            works.append(work)
        return works

    def search_author(self, author: str, limit: int = 12) -> list[Work]:
        return self.search(author, limit)

    def close(self) -> None:
        pass
