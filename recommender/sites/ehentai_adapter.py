# -*- coding: utf-8 -*-
"""E-Hentai 适配器：搜索（列表页解析）+ gdata 元数据 + popular 热作。"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..config import ROOT
from ..http_client import HttpClient, HttpError
from .base import SiteAdapter, Work

_ITEM_RE = re.compile(
    r'<a href="https://e-hentai\.org/g/(\d+)/([0-9a-f]{10})/"\s*><div class="glink">(.*?)</div>',
    re.S,
)
_CAT_RE = re.compile(r'<div class="cn ct\d+"[^>]*>([^<]*)</div>')
_PAGES_RE = re.compile(r"<div>(\d+)\s*pages</div>")
_TOTAL_RE = re.compile(r"Found about\s*([\d,]+)\s*results")
_THUMB_RE = re.compile(r'src="(https?://(?:ehgt|exhentai)\.org/[^"]+)"')

_CACHE_PATH = ROOT / "data" / "metadata" / "_gdata_cache.json"


def _parse_items(text: str) -> list[dict]:
    items = []
    for m in _ITEM_RE.finditer(text):
        items.append(
            {
                "gid": int(m.group(1)),
                "token": m.group(2),
                "title": re.sub(r"<[^>]+>", "", m.group(3)).strip(),
            }
        )
    chunks = re.split(r"<tr>", text)
    by_gid: dict[int, str] = {}
    for ch in chunks:
        lm = re.search(r'href="https://e-hentai\.org/g/(\d+)/', ch)
        if lm:
            by_gid[int(lm.group(1))] = ch
    for it in items:
        ch = by_gid.get(it["gid"], "")
        cm = _CAT_RE.search(ch)
        if cm:
            it["category"] = cm.group(1).strip()
        pm = _PAGES_RE.search(ch)
        if pm:
            it["pages"] = int(pm.group(1))
        tm = _THUMB_RE.search(ch)
        if tm:
            it["thumb"] = tm.group(1)
    return items


def _total_of(text: str) -> int | None:
    m = _TOTAL_RE.search(text)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:  # noqa: BLE001
        return None


class EhentaiAdapter(SiteAdapter):
    name = "e-hentai"
    domain = "e-hentai.org"

    def __init__(self) -> None:
        super().__init__()
        self.client = HttpClient()

    def _probe(self) -> bool:
        try:
            r = self.client.get("https://api.e-hentai.org/api.php", self.domain)
            return r.status_code == 200
        except HttpError:
            return False

    def _search_items(self, url: str) -> list[dict]:
        r = self.client.get(url, self.domain)
        return _parse_items(r.text)

    def gdata(self, gid: int, token: str) -> dict | None:
        cache = {}
        if _CACHE_PATH.exists():
            try:
                cache = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                cache = {}
        if str(gid) in cache:
            return cache[str(gid)]
        r = self.client.post_json(
            "https://api.e-hentai.org/api.php",
            self.domain,
            {"method": "gdata", "gidlist": [[gid, token]], "namespace": 1},
        )
        try:
            meta = (r.json().get("gmetadata") or [None])[0]
        except Exception:  # noqa: BLE001
            return None
        if not meta:
            return None
        rating = meta.get("rating")
        out = {
            "gid": meta.get("gid"),
            "token": meta.get("token"),
            "title_en": meta.get("title"),
            "title_jp": meta.get("title_jpn"),
            "category": meta.get("category"),
            "uploader": meta.get("uploader"),
            "posted": meta.get("posted"),
            "filecount": int(meta.get("filecount") or 0),
            "rating": float(rating) if rating else None,
            "tags": meta.get("tags", []),
            "thumb": meta.get("thumb") or "",
        }
        cache[str(gid)] = out
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        return out

    def batch_thumbs(self, items: list[tuple[int, str]]) -> dict[str, str]:
        """一次请求批量取封面：[(gid, token), ...] -> {str(gid): thumb_url}。"""
        if not items:
            return {}
        r = self.client.post_json(
            "https://api.e-hentai.org/api.php",
            self.domain,
            {"method": "gdata", "gidlist": [[g, t] for g, t in items], "namespace": 1},
        )
        try:
            metas = r.json().get("gmetadata") or []
        except Exception:  # noqa: BLE001
            return {}
        out: dict[str, str] = {}
        for m in metas:
            gid = m.get("gid")
            thumb = m.get("thumb") or ""
            if gid is not None:
                out[str(gid)] = thumb
                # 顺手补进缓存（无 thumb 字段差异无妨）
                cache = {}
                if _CACHE_PATH.exists():
                    try:
                        cache = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
                    except Exception:  # noqa: BLE001
                        cache = {}
                if str(gid) not in cache:
                    cache[str(gid)] = {"gid": gid, "token": m.get("token"),
                                       "title_en": m.get("title"),
                                       "title_jp": m.get("title_jpn"),
                                       "category": m.get("category"),
                                       "posted": m.get("posted"),
                                       "filecount": int(m.get("filecount") or 0),
                                       "rating": float(m.get("rating") or 0) if m.get("rating") else None,
                                       "tags": m.get("tags", []),
                                       "thumb": thumb}
                _CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        return out

    def _to_work(self, item: dict, meta: dict | None) -> Work:
        title = (meta or {}).get("title_en") or item["title"]
        tags = (meta or {}).get("tags", []) or []
        author = ""
        m = re.match(r"^\[([^\[\]]+)\]", title)
        if m:
            head = m.group(1)
            if "(" in head and head.endswith(")"):
                _, _, artist = head.rpartition("(")
                author = artist.rstrip(")").strip()
            else:
                author = head
        return Work(
            site=self.name,
            work_id=str(item["gid"]),
            title=title,
            author=author,
            tags=tags,
            category=(meta or {}).get("category") or item.get("category", ""),
            rating=float((meta or {}).get("rating") or 0.0),
            rating_count=0,
            pages=int((meta or {}).get("filecount") or item.get("pages") or 0),
            url=f"https://e-hentai.org/g/{item['gid']}/{item['token']}/",
            posted=str((meta or {}).get("posted") or ""),
            is_chinese=any(str(t).startswith("language:chinese") for t in tags),
            raw={"gid": item["gid"], "token": item["token"]},
        )

    def search_light(self, query: str, limit: int = 6, author_mode: bool = False) -> list[Work]:
        """轻量搜索：只取列表页标题与链接，不调 gdata。raw["total"] 为站点报告的总条数。"""
        from urllib.parse import quote

        # 多词作者名必须整段引号包裹，否则空格会把查询拆成 artist:xxx + 单词
        q = f'artist:"{query}$"' if author_mode else query
        url = f"https://e-hentai.org/?f_search={quote(q)}&f_cats=0"
        r = self.client.get(url, self.domain)
        items = _parse_items(r.text)[:limit]
        total = _total_of(r.text)
        if author_mode and not items:
            # 标签式精确匹配没命中（作者名写法不同等），退回文本搜索兜底
            url = f"https://e-hentai.org/?f_search={quote(query)}&f_cats=0"
            r = self.client.get(url, self.domain)
            items = _parse_items(r.text)[:limit]
            total = _total_of(r.text)
        return [
            Work(
                site=self.name,
                work_id=str(it["gid"]),
                title=it["title"],
                url=f"https://e-hentai.org/g/{it['gid']}/{it['token']}/",
                pages=it.get("pages") or 0,
                raw={"total": total, "cover": it.get("thumb") or ""},
            )
            for it in items
        ]

    def search(self, query: str, limit: int = 15) -> list[Work]:
        from urllib.parse import quote

        url = f"https://e-hentai.org/?f_search={quote(query)}&f_cats=0"
        items = self._search_items(url)[:limit]
        works = []
        for it in items:
            meta = self.gdata(it["gid"], it["token"])
            works.append(self._to_work(it, meta))
        return works

    def search_author(self, author: str, limit: int = 15) -> list[Work]:
        from urllib.parse import quote

        # EH 的 artist 标签搜索（罗马音作者名），搜不到再退回文本搜索
        try:
            url = f"https://e-hentai.org/?f_search={quote('artist:\"' + author + '$\"')}&f_cats=0"
            items = self._search_items(url)[:limit]
        except HttpError:
            items = []
        if not items:
            return self.search(author, limit)
        works = []
        for it in items:
            meta = self.gdata(it["gid"], it["token"])
            works.append(self._to_work(it, meta))
        return works

    def trending(self, limit: int = 15) -> list[Work]:
        items = self._search_items("https://e-hentai.org/popular")[:limit]
        works = []
        for it in items:
            meta = self.gdata(it["gid"], it["token"])
            works.append(self._to_work(it, meta))
        return works
