# -*- coding: utf-8 -*-
"""信息增强：为推荐条目回填 收藏数（EH Favorited）与 简介（wnacg 簡介 / 禁漫详情）。"""
from __future__ import annotations

import json
import re

from .db import get_conn, init_db
from .http_client import HttpClient, HttpError

_FAV_RE = re.compile(r'id="favcount">([\d,]+)\s*times')
_WN_DESC_RE = re.compile(r"簡介：(.*?)</p>", re.S)


def _strip_html(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s)
    return re.sub(r"<[^>]+>", "", s).strip()


def regen_reasons() -> int:
    """用鉴赏式短文重新生成全部推荐理由（含简介/收藏数等增强数据）。"""
    from .ai_assist import AIClient
    from .library import get_aliases, library_items

    ai = AIClient()
    if not ai.available:
        print("AI 不可用。")
        return 1
    lib = library_items()
    author_counts = {g["author"]: g["count"] for g in lib["groups"]}
    aliases = get_aliases()
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM recommended ORDER BY score DESC")]
    ok = 0
    for r in rows:
        canon = aliases.canonical(r["author"] or "")
        facts = {
            "title": r["title"],
            "author": r["author"],
            "channel": r["channel"],
            "rating": r["rating"],
            "rating_count": r["rating_count"],
            "pages": r["pages"],
            "tags": (json.loads(r["tags"] or "[]") or [])[:12],
            "description": r.get("description") or "",
        }
        reason = ai.write_reason(facts)
        if reason:
            with get_conn() as conn:
                conn.execute("UPDATE recommended SET reason=? WHERE work_id=?",
                             (reason, r["work_id"]))
                conn.commit()
            ok += 1
            print(f"  ✓ {r['title'][:38]}")
        else:
            print(f"  ✗ 失败 {r['title'][:30]}")
    print(f"理由重生成: {ok}/{len(rows)}")
    return 0


def complete_search_favs() -> int:
    """补全"搜索收藏"条目的元数据与推荐理由（更新推荐时自动执行）。

    按站点取：EH gdata+收藏数 / nhentai v2 详情 / wnacg 簡介 / 禁漫详情；
    有标签后生成鉴赏理由。"""
    from .ai_assist import AIClient

    with get_conn() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM recommended WHERE channel='search' AND feedback='like'"
            )
        ]
    if not rows:
        return 0
    ai = AIClient()
    client = HttpClient()
    done = 0
    for r in rows:
        site = r["primary_site"]
        links = json.loads(r["links"] or "[]")
        url = links[0].get("url", "") if links else ""
        tags = json.loads(r["tags"] or "[]")
        desc = r.get("description") or ""
        favs = r.get("rating_count") or 0
        author = r.get("author") or ""
        pages = r.get("pages") or 0
        rating = r.get("rating") or 0
        cover = r.get("cover_url") or ""
        try:
            if site == "e-hentai":
                m = re.search(r"/g/(\d+)/([0-9a-f]{10})/", url)
                if m:
                    from .sites.ehentai_adapter import EhentaiAdapter

                    meta = EhentaiAdapter().gdata(int(m.group(1)), m.group(2))
                    if meta:
                        tags = meta.get("tags") or tags
                        pages = meta.get("filecount") or pages
                        rating = meta.get("rating") or rating
                        if meta.get("thumb"):
                            cover = cover or f"https://ehgt.org/{meta['thumb']}"
                        if not author:
                            from .parser import extract_author_from_title

                            author = extract_author_from_title(meta.get("title_en") or "")
                    resp = client.get(f"https://e-hentai.org/g/{m.group(1)}/{m.group(2)}/",
                                      "e-hentai.org")
                    fm = re.search(r'id="favcount">([\d,]+)\s*times', resp.text)
                    if fm:
                        favs = int(fm.group(1).replace(",", ""))
            elif site == "nhentai":
                from .sites.nhentai_adapter import NhentaiAdapter

                ad = NhentaiAdapter()
                detail = ad._get_json(f"https://nhentai.net/api/v2/galleries/{r['work_id'].split(':', 1)[1]}")
                w = ad._to_work(detail, detail.get("tags") or [])
                tags = w.tags or tags
                author = w.author or author
                pages = w.pages or pages
                favs = w.rating_count or favs
                thumb = detail.get("thumbnail")
                p = thumb.get("path") if isinstance(thumb, dict) else thumb
                if not p:
                    img = (detail.get("images") or {}).get("thumbnail") or {}
                    p = img.get("path") if isinstance(img, dict) else ""
                if p:
                    cover = cover or ("https://t.nhentai.net/" + p)
            elif site == "wnacg":
                m = re.search(r"photos-index-aid-(\d+)", url)
                if m:
                    resp = client.get(f"https://www.wnacg.com/photos-index-aid-{m.group(1)}.html",
                                      "wnacg.com")
                    dm = re.search(r"簡介：(.*?)</p>", resp.text, re.S)
                    if dm:
                        desc = re.sub(r"<br\s*/?>", "\n", dm.group(1))
                        desc = re.sub(r"<[^>]+>", "", desc).strip()[:800]
            elif site == "18comic":
                from .sites.jm18_adapter import JM18Adapter, _TAG_MAP

                d = JM18Adapter()._ensure().get_album_detail(
                    r["work_id"].split(":", 1)[1])
                if getattr(d, "tags", None):
                    tags = [t for t in d.tags if isinstance(t, str) and t.strip()]
                    for t in list(tags):
                        mapped = _TAG_MAP.get(t)
                        if mapped and mapped not in tags:
                            tags.append(mapped)
                if str(getattr(d, "description", "") or "").strip():
                    desc = str(d.description).strip()[:800]
                if getattr(d, "likes", 0):
                    favs = int(d.likes)
                if getattr(d, "page_count", 0):
                    pages = int(d.page_count)
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {r['title'][:30]} 补全失败: {type(e).__name__} {str(e)[:60]}")

        reason = r.get("reason") or ""
        if not reason and ai.available and tags:
            facts = {
                "title": r["title"], "author": author, "channel": "search",
                "rating": rating, "rating_count": favs, "pages": pages,
                "tags": tags[:12], "description": desc,
            }
            reason = ai.write_reason(facts) or ""
        with get_conn() as conn:
            conn.execute(
                """UPDATE recommended SET tags=?, author=?, pages=?, rating_count=?,
                   rating=CASE WHEN rating>0 THEN rating ELSE ? END, description=?,
                   reason=CASE WHEN reason IS NULL OR reason='' THEN ? ELSE reason END,
                   cover_url=CASE WHEN cover_url IS NULL OR cover_url='' THEN ? ELSE cover_url END
                   WHERE work_id=?""",
                (json.dumps(tags, ensure_ascii=False), author, pages, favs, rating,
                 desc, reason, cover, r["work_id"]),
            )
            conn.commit()
        done += 1
        print(f"  ✓ {r['title'][:38]} 已补全（标签 {len(tags)} 个，理由 {'有' if reason else '无'}）")
    print(f"搜索收藏补全: {done}/{len(rows)} 条")
    return 0


def run() -> int:
    init_db()
    client = HttpClient()
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM recommended ORDER BY score DESC")]
    fixed = 0
    for r in rows:
        links = json.loads(r["links"] or "[]")
        desc = r.get("description") or ""
        fav_count = r.get("rating_count") or 0
        # EH：Favorited 收藏数（简介新版页面不输出）
        if not fav_count:
            for l in links:
                if l.get("site") != "e-hentai":
                    continue
                m = re.search(r"/g/(\d+)/([0-9a-f]{10})/", l.get("url", ""))
                if not m:
                    continue
                try:
                    resp = client.get(f"https://e-hentai.org/g/{m.group(1)}/{m.group(2)}/", "e-hentai.org")
                    fm = _FAV_RE.search(resp.text)
                    if fm:
                        fav_count = int(fm.group(1).replace(",", ""))
                    break
                except HttpError:
                    continue
        # wnacg：簡介段落
        if not desc:
            for l in links:
                if l.get("site") != "wnacg":
                    continue
                m = re.search(r"photos-index-aid-(\d+)", l.get("url", ""))
                if not m:
                    continue
                try:
                    resp = client.get(f"https://www.wnacg.com/photos-index-aid-{m.group(1)}.html", "wnacg.com")
                    dm = _WN_DESC_RE.search(resp.text)
                    if dm:
                        desc = _strip_html(dm.group(1))[:800]
                    break
                except HttpError:
                    continue
        # 18comic：详情简介
        if not desc:
            for l in links:
                if l.get("site") != "18comic":
                    continue
                m = re.search(r"/album/(\d+)", l.get("url", ""))
                if not m:
                    continue
                try:
                    import jmcomic

                    jmcomic.disable_jm_log()
                    option = jmcomic.JmOption.default()
                    d = option.new_jm_client(impl="api").get_album_detail(m.group(1))
                    raw_desc = getattr(d, "description", "") or ""
                    if raw_desc and str(raw_desc).strip():
                        desc = str(raw_desc).strip()[:800]
                    break
                except Exception:  # noqa: BLE001
                    continue
        if desc or fav_count:
            with get_conn() as conn:
                conn.execute(
                    "UPDATE recommended SET description=?, rating_count=CASE WHEN rating_count>0 "
                    "THEN rating_count ELSE ? END WHERE work_id=?",
                    (desc, fav_count, r["work_id"]),
                )
                conn.commit()
            fixed += 1
            print(f"  ✓ {r['title'][:38]} | favs={fav_count or '-'} | 简介={'有' if desc else '无'}")
    print(f"增强完成: {fixed}/{len(rows)} 条")
    return 0
