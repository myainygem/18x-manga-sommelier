# -*- coding: utf-8 -*-
"""收藏库与作者别名合并。

作者别名：同一作者可能有多个名字（日文名/罗马音/artist 标签名），
通过「条目社团名 ↔ 画廊 artist: 标签」配对 + 罗马音模糊匹配做并查集合并，
得到规范名（canonical）。
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from difflib import SequenceMatcher

from .config import ROOT
from .db import get_conn
from .parser import norm_key, to_romaji


class AuthorAliases:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}
        self.counts: Counter[str] = Counter()
        self._canonical: dict[str, str] | None = None

    def _find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def _union(self, a: str, b: str) -> None:
        ra, rb = self._find(a), self._find(b)
        if ra != rb:
            self.parent[ra] = rb

    def build(self) -> "AuthorAliases":
        pairs: list[tuple[str, str]] = []
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT e.circle_clean, e.artist, m.tags FROM entries e
                   JOIN eh_metadata m ON m.entry_id = e.id
                   WHERE m.tags IS NOT NULL AND m.chosen_by NOT IN ('manual','skip')"""
            )
            for circle, artist, tags in rows:
                name = (artist or circle or "").strip()
                if name:
                    self.counts[name] += 1
                if not name:
                    continue
                for t in json.loads(tags or "[]"):
                    if t.startswith("artist:"):
                        an = t.split(":", 1)[1].strip()
                        if an:
                            pairs.append((name, an))
        for a, b in pairs:
            self._union(a, b)
        # 罗马音模糊合并（不同条目里同一作者的不同拼写）
        keys = list(self.parent.keys())
        norm_of: dict[str, str] = {}
        for k in keys:
            r = norm_key(to_romaji(k)) or norm_key(k)
            norm_of[k] = r
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                a, b = keys[i], keys[j]
                if self._find(a) == self._find(b):
                    continue
                na, nb = norm_of[a], norm_of[b]
                if not na or not nb:
                    continue
                if na == nb or (len(na) >= 5 and len(nb) >= 5
                                and SequenceMatcher(None, na, nb).ratio() >= 0.8):
                    self._union(a, b)
        return self

    def groups(self) -> list[list[str]]:
        grouped: dict[str, list[str]] = defaultdict(list)
        for k in self.parent:
            grouped[self._find(k)].append(k)
        return list(grouped.values())

    def canonical_map(self) -> dict[str, str]:
        """别名 → 规范名（规范名取库中出现次数最多者，其次优先含日文的名字）。"""
        if self._canonical is not None:
            return self._canonical
        self._canonical = {}
        for group in self.groups():
            if not group:
                continue
            best = max(
                group,
                key=lambda n: (
                    self.counts.get(n, 0),
                    1 if any("\u3040" <= ch <= "\u30ff" or "\u4e00" <= ch <= "\u9fff" for ch in n) else 0,
                    -len(n),
                ),
            )
            for name in group:
                self._canonical[name] = best
        return self._canonical

    def canonical(self, name: str) -> str:
        name = (name or "").strip()
        if not name:
            return name
        return self.canonical_map().get(name, name)


_ALIASES: AuthorAliases | None = None


def get_aliases() -> AuthorAliases:
    global _ALIASES
    if _ALIASES is None:
        _ALIASES = AuthorAliases().build()
    return _ALIASES


def load_thumbs() -> dict[str, str]:
    """读封面缓存 data/library_thumbs.json: {gid: thumb_url}。"""
    path = ROOT / "data" / "library_thumbs.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def fetch_library_thumbs() -> None:
    """后台批量抓取收藏库封面（gdata 一次 25 个），写入 data/library_thumbs.json。"""
    from .sites.ehentai_adapter import EhentaiAdapter
    from .db import get_conn as _get_conn

    with _get_conn() as conn:
        rows = [
            (r["gid"], r["token"])
            for r in conn.execute(
                """SELECT m.gid, m.token FROM eh_metadata m
                   WHERE m.chosen_by NOT IN ('manual','skip') AND m.gid IS NOT NULL"""
            )
        ]
    if not rows:
        return
    thumbs = load_thumbs()
    missing = [(g, t) for g, t in rows if str(g) not in thumbs]
    if not missing:
        return
    adapter = EhentaiAdapter()
    for i in range(0, len(missing), 25):
        chunk = missing[i:i + 25]
        try:
            got = adapter.batch_thumbs(chunk)
        except Exception:  # noqa: BLE001
            continue
        for gid, url in got.items():
            if url:
                thumbs[str(gid)] = url
    path = ROOT / "data" / "library_thumbs.json"
    path.write_text(json.dumps(thumbs, ensure_ascii=False), encoding="utf-8")


def library_items() -> dict:
    """用户收藏库（已补全元数据部分），按规范作者分组。

    返回: {"groups": [{"author": str, "count": int, "items": [ {...} ]}]}
    """
    aliases = get_aliases()
    thumbs = load_thumbs()
    groups: dict[str, list[dict]] = defaultdict(list)
    seen_gids: set[str] = set()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT e.line_no, e.circle_clean, e.artist, e.title_clean, e.uncensored, e.lang,
                      m.gid, m.token, m.title_en, m.rating, m.pages, m.tags, m.category
               FROM entries e
               JOIN eh_metadata m ON m.entry_id = e.id
               WHERE m.chosen_by NOT IN ('manual','skip') AND m.gid IS NOT NULL
                 AND COALESCE(m.removed,0)=0
               ORDER BY e.line_no"""
        )
        for r in rows:
            gid = str(r["gid"] or "")
            if gid in seen_gids:  # 同一作品只计一次（与画像的 314 部口径一致）
                continue
            seen_gids.add(gid)
            tags = json.loads(r["tags"] or "[]")
            name = (r["artist"] or r["circle_clean"] or "").strip()
            if not name:
                # 文件夹名没写作者时，回退到画廊的 artist: 标签
                for t in tags:
                    if t.startswith("artist:"):
                        name = t.split(":", 1)[1].strip()
                        break
            if not name:
                name = "未知作者"
            canonical = aliases.canonical(name)
            item = {
                "line_no": r["line_no"],
                "title": r["title_clean"] or r["title_en"] or "",
                "author": name,
                "rating": r["rating"],
                "pages": r["pages"],
                "category": r["category"] or "",
                "uncensored": bool(r["uncensored"]),
                "zh": r["lang"] == "zh",
                "tags": tags,
                "gid": gid,
                "cover": thumbs.get(gid, ""),
                "url": f"https://e-hentai.org/g/{r['gid']}/{r['token']}/"
                if r["gid"] and r["token"] else "",
            }
            groups[canonical].append(item)
    group_list = [
        {"author": author, "count": len(items), "items": items}
        for author, items in groups.items()
    ]
    # 手点收藏的推荐也并入收藏列表（与导入的收藏一视同仁）
    from .db import get_conn as _conn2

    with _conn2() as conn:
        liked = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM recommended WHERE feedback='like' ORDER BY score DESC"
            )
        ]
    from .parser import norm_key

    for r in liked:
        author = (r["author"] or "未知作者").strip()
        canonical = aliases.canonical(author)
        title = r["title"] or ""
        items = groups.get(canonical)
        if items is not None and any(
            norm_key(i["title"]) == norm_key(title) for i in items
        ):
            continue  # 已存在于库中（同一部作品），不重复加入
        item = {
            "line_no": None,
            "title": title,
            "author": author,
            "rating": r["rating"],
            "rating_count": r["rating_count"],
            "pages": r["pages"],
            "category": "",
            "uncensored": False,
            "zh": r["lang"] == "zh",
            "tags": json.loads(r["tags"] or "[]"),
            "gid": "",
            "work_id": r["work_id"],
            "cover": r["cover_url"] or "",
            "url": (json.loads(r["links"] or "[]") or [{}])[0].get("url", ""),
            "links": json.loads(r["links"] or "[]"),
            "fav": True,
            # 收藏时把推荐里收集的完整信息一并保存展示
            "description": r["description"] or "",
            "reason": r["reason"] or "",
        }
        groups[canonical].append(item)
    group_list = [
        {"author": author, "count": len(items), "items": items}
        for author, items in groups.items()
    ]
    group_list.sort(key=lambda g: (-g["count"], g["author"]))
    return {
        "total": sum(g["count"] for g in group_list),
        "author_count": len(group_list),
        "groups": group_list,
    }
