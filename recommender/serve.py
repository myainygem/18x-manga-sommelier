# -*- coding: utf-8 -*-
"""本地推荐 UI：HTTP 服务 + 玻璃拟态单页应用。

接口:
  GET  /                UI 页面
  GET  /api/items       推荐（已排除"不感兴趣"）
  GET  /api/library     收藏库（按合并后的作者分组）
  GET  /api/status      画像与统计
  GET  /api/config      UI 配置（预设搜索站）
  POST /api/config      {"search_site": "ehentai|nhentai|wnacg|18comic"}
  POST /api/feedback    {"work_id": "...", "choice": "like"|"meh"}
"""
from __future__ import annotations

import json
import re
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import CONFIG_PATH, ROOT, config
from .db import get_conn, init_db
from .feedback import apply_feedback, record
from .library import library_items

LIKED_PATH = ROOT / "data" / "liked.json"

CHANNEL_TITLES = {"precision": "精准推荐", "trending": "热作推荐", "series": "连载追踪"}

SEARCH_SITES = {
    "ehentai": {"label": "E-Hentai", "enabled": True},
    "nhentai": {"label": "nHentai", "enabled": True},
    "wnacg": {"label": "绅士漫画", "enabled": True},
    "18comic": {"label": "禁漫", "enabled": True},
}

_TAG_ZH: dict | None = None


def tag_zh() -> dict:
    global _TAG_ZH
    if _TAG_ZH is None:
        path = ROOT / "data" / "tag_zh.json"
        if path.exists():
            try:
                _TAG_ZH = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                _TAG_ZH = {}
        else:
            _TAG_ZH = {}
    return _TAG_ZH


def _attach_tag_pairs(items: list[dict]) -> list[dict]:
    zh = tag_zh()
    NS_CN = {"artist": "作者", "parody": "原作", "character": "角色",
             "group": "社团", "language": "语言"}
    for it in items:
        pairs = []
        for t in (it.get("tags") or []):
            if not t:
                continue
            ns, _, name = t.partition(":")
            zhname = zh.get(t) or zh.get(name) or name
            # 去掉翻译结果里可能带有的命名空间前缀（如"男性:巨乳"→"巨乳"）
            zhname = re.sub(r"^(?:[a-z]+|[\u4e00-\u9fff]{1,4})\s*[::]\s*", "", zhname)
            if ns in NS_CN:
                display = f"{NS_CN[ns]}·{zhname}"
            else:
                display = zhname
            pairs.append({"orig": t, "display": display})
        it["tags_pairs"] = pairs
    return items


def _liked_list() -> list[dict]:
    if LIKED_PATH.exists():
        try:
            return json.loads(LIKED_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return []
    return []


def _add_liked(item: dict) -> None:
    liked = _liked_list()
    if any(x.get("work_id") == item["work_id"] for x in liked):
        return
    liked.append(
        {
            "work_id": item["work_id"],
            "title": item["title"],
            "author": item.get("author") or "",
            "links": item.get("links") or [],
            "tags": item.get("tags") or [],
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
    )
    LIKED_PATH.parent.mkdir(parents=True, exist_ok=True)
    LIKED_PATH.write_text(json.dumps(liked, ensure_ascii=False, indent=2), encoding="utf-8")


def _remove_liked(work_id: str) -> None:
    liked = [x for x in _liked_list() if x.get("work_id") != work_id]
    if len(liked) != len(_liked_list()):
        LIKED_PATH.write_text(json.dumps(liked, ensure_ascii=False, indent=2), encoding="utf-8")


def get_items() -> list[dict]:
    init_db()
    with get_conn() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM recommended WHERE (feedback IS NULL OR feedback='like') "
                "AND channel != 'search' AND COALESCE(superseded,0)=0 "
                "ORDER BY "
                "CASE channel WHEN 'precision' THEN 1 WHEN 'trending' THEN 2 ELSE 3 END, "
                "score DESC"
            )
        ]
    for r in rows:
        r["tags"] = json.loads(r["tags"] or "[]")
        r["links"] = json.loads(r["links"] or "[]")
        r["liked"] = r["feedback"] == "like"
        r["meh"] = False
    _attach_tag_pairs(rows)
    return rows


def get_status() -> dict:
    profile = {}
    p_path = ROOT / "data" / "profile" / "preference_profile.json"
    if p_path.exists():
        try:
            profile = json.loads(p_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            profile = {}
    with get_conn() as conn:
        n_rec = conn.execute("SELECT COUNT(*) FROM recommended").fetchone()[0]
        n_like = conn.execute("SELECT COUNT(*) FROM recommended WHERE feedback='like'").fetchone()[0]
        n_meh = conn.execute("SELECT COUNT(*) FROM recommended WHERE feedback='meh'").fetchone()[0]
        n_hide = conn.execute("SELECT COUNT(*) FROM recommended WHERE feedback='hide'").fetchone()[0]
    rating = profile.get("rating", {}) or {}
    lib = library_items()
    return {
        "total_works": profile.get("total_works", 0),
        "mean_rating": rating.get("mean"),
        "zh_ratio": profile.get("zh_ratio", 0),
        "n_rec": n_rec,
        "n_like": n_like,
        "n_meh": n_meh,
        "n_hide": n_hide,
        "n_library": lib.get("total", 0),
        # 统一"收藏"口径：导入的收藏 + 手点收藏（library_items 已合并去重）
        "n_fav": lib.get("total", 0),
        "top_authors": [a["name"] for a in (profile.get("authors") or [])[:5]],
        "top_tags": [t["tag"] for t in (profile.get("tags") or [])[:6]],
        "liked_total": len(_liked_list()),
        # V1：数据/配置目录（点击可打开，便于填写 config.json 与放置 cookie 文件）
        "data_dir": str(ROOT),
    }


def get_config() -> dict:
    return {
        "search_site": config.get("ui", {}).get("search_site", "ehentai"),
        "options": SEARCH_SITES,
    }


def set_search_site(site: str) -> dict:
    if site not in SEARCH_SITES:
        return {"ok": False, "error": "未知站点"}
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    data.setdefault("ui", {})["search_site"] = site
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    config.data.setdefault("ui", {})["search_site"] = site
    return {"ok": True, "search_site": site}


# 更新推荐（后台运行 + 进度捕获）
_REFRESH = {"running": False, "lines": [], "code": None}
_REFRESH_LOCK = threading.Lock()


def refresh_status() -> dict:
    with _REFRESH_LOCK:
        return dict(_REFRESH)


_SITES_CACHE: dict = {"data": None, "ts": 0.0}
_SITES_CACHE_LOCK = threading.Lock()


def _probe_sites() -> dict:
    from .sites import available_adapters, get_adapters

    labels = {"e-hentai": "E-Hentai", "nhentai": "nHentai", "wnacg": "绅士漫画",
              "18comic": "禁漫", "hitomi": "hitomi"}
    adapters = get_adapters()
    avail = {a.name for a in available_adapters()}
    return {
        "sites": [
            {"name": a.name, "label": labels.get(a.name, a.name),
             "available": a.name in avail}
            for a in adapters
        ],
        "default": [a.name for a in adapters if a.name in avail],
    }


def api_sites() -> dict:
    """站点清单（缓存，10 分钟刷新一次；启动时后台预热）。"""
    with _SITES_CACHE_LOCK:
        data = _SITES_CACHE.get("data")
    if data is None:
        data = _probe_sites()
        with _SITES_CACHE_LOCK:
            _SITES_CACHE["data"] = data
            _SITES_CACHE["ts"] = time.time()
    return data


def _sites_cache_loop() -> None:
    # 启动即预热一次，之后每 10 分钟后台刷新
    while True:
        try:
            data = _probe_sites()
            with _SITES_CACHE_LOCK:
                _SITES_CACHE["data"] = data
                _SITES_CACHE["ts"] = time.time()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(600)


def start_refresh(sites: list[str] | None = None) -> dict:
    with _REFRESH_LOCK:
        if _REFRESH["running"]:
            return {"ok": False, "error": "更新任务已在运行中"}
        _REFRESH["running"] = True
        _REFRESH["lines"] = [
            f"推荐生成任务已启动（约 20-40 分钟）"
            + (f"；站点：{', '.join(sites)}" if sites else "")
            + " …"
        ]
        _REFRESH["code"] = None

    def worker() -> None:
        import contextlib
        import io

        buf = io.StringIO()

        class Tee:
            def write(self, s):  # noqa: D102
                buf.write(s)
                with _REFRESH_LOCK:
                    for line in s.splitlines():
                        line = line.strip()
                        if line:
                            _REFRESH["lines"].append(line)
                            if len(_REFRESH["lines"]) > 200:
                                _REFRESH["lines"] = _REFRESH["lines"][-200:]

            def flush(self):  # noqa: D102
                pass

        code = -1
        with contextlib.redirect_stdout(Tee()):
            try:
                from .recommend import run as rec_run

                code = rec_run(sites=sites)
            except Exception as e:  # noqa: BLE001
                with _REFRESH_LOCK:
                    _REFRESH["lines"].append(f"错误: {type(e).__name__} {e}")
        with _REFRESH_LOCK:
            _REFRESH["running"] = False
            _REFRESH["code"] = code
            _REFRESH["lines"].append(f"任务结束（exit {code}）")

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True}


def api_search(q: str, author_mode: bool = False) -> dict:
    """跨站搜索：名字/作者名 → 各站结果（标题/作者/链接/封面）。"""
    q = (q or "").strip()
    if not q:
        return {"ok": False, "error": "搜索词为空"}
    from .sites import available_adapters

    adapters = available_adapters()
    results: list[dict] = []
    errors: list[dict] = []

    def worker(ad) -> None:
        try:
            fn = getattr(ad, "search_light", None)
            items = fn(q, 25, author_mode) if fn else ad.search(q, 25)
            total = None
            for it in items:
                t = (it.raw or {}).get("total")
                if isinstance(t, int) and t > 0:
                    total = t
                    break
            results.append(
                {
                    "site": ad.name,
                    "total": total,
                    "items": [
                        {
                            "id": it.work_id,
                            "title": it.title,
                            "author": it.author,
                            "url": it.url,
                            "cover": (
                                "https://t.nhentai.net/" + str(it.raw.get("thumb"))
                                if ad.name == "nhentai" and it.raw.get("thumb")
                                else (it.raw.get("cover") or "")
                            ),
                        }
                        for it in items
                    ],
                }
            )
        except Exception as e:  # noqa: BLE001
            errors.append({"site": ad.name, "error": str(e)[:100]})
        finally:
            close = getattr(ad, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass

    threads = [threading.Thread(target=worker, args=(ad,), daemon=True) for ad in adapters]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    order = {"e-hentai": 0, "nhentai": 1, "wnacg": 2, "18comic": 3}
    results.sort(key=lambda r: order.get(r["site"], 9))
    return {"ok": True, "results": results, "errors": errors}


def search_fav(body: dict) -> dict:
    """搜索页收藏：全新条目落库为 search 通道；已存在条目（如推荐）只补收藏标记，
    绝不覆盖原有的标签/评分/理由/频道等信息。"""
    site = body.get("site")
    work_id = str(body.get("id", "")).strip()
    title = (body.get("title") or "").strip()
    if not site or not work_id or not title:
        return {"ok": False, "error": "参数不完整"}
    url = body.get("url") or ""
    cover = body.get("cover") or ""
    tags = body.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    wid = f"{site}:{work_id}"
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM recommended WHERE work_id=?", (wid,)).fetchone()
        if row:
            row = dict(row)
            if row["feedback"] == "like":
                # 已收藏 → 取消（条目本身保留，原推荐条目会回到推荐页）
                conn.execute("UPDATE recommended SET feedback=NULL, feedback_at=NULL, "
                             "feedback_applied=0 WHERE work_id=?", (wid,))
                conn.commit()
                _remove_liked(wid)
                return {"ok": True, "unliked": True}
            # 已有条目：只标记收藏 + 补缺失的封面/链接，其余字段原样保留
            upd = ["feedback='like'", "feedback_at=NULL", "feedback_applied=0", "superseded=0"]
            params: list = []
            if not row["cover_url"] and cover:
                upd.append("cover_url=?")
                params.append(cover)
            if not row["links"] and url:
                upd.append("links=?")
                params.append(json.dumps([{"site": site, "url": url}], ensure_ascii=False))
            params.append(wid)
            conn.execute(f"UPDATE recommended SET {', '.join(upd)} WHERE work_id=?", params)
            conn.commit()
            _add_liked({
                "work_id": wid,
                "title": row["title"] or title,
                "author": row.get("author") or "",
                "links": json.loads(row["links"] or "[]"),
                "tags": json.loads(row["tags"] or "[]"),
            })
            return {"ok": True, "faved": True}
        conn.execute(
            """INSERT INTO recommended
               (work_id, primary_site, title, author, tags, rating, rating_count, pages, lang,
                channel, score, reason, links, first_seen, cover_url, feedback, feedback_at,
                feedback_applied, description)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                wid, site, title, body.get("author") or "", json.dumps(tags, ensure_ascii=False),
                0, 0, 0, "zh", "search", 0, "",
                json.dumps([{"site": site, "url": url}], ensure_ascii=False),
                datetime.now().isoformat(timespec="seconds"), cover, "like", None, 0, "",
            ),
        )
        conn.commit()
    _add_liked({"work_id": wid, "title": title, "author": body.get("author") or "",
                "links": [{"site": site, "url": url}], "tags": tags})
    return {"ok": True, "faved": True}


def unlibrary(body: dict) -> dict:
    """收藏列表：移出/恢复本地导入的收藏（eh_metadata.removed 标记，可撤回）。"""
    gid = str(body.get("gid", "")).strip()
    undo = bool(body.get("undo", False))
    if not gid:
        return {"ok": False, "error": "缺少 gid"}
    with get_conn() as conn:
        conn.execute("UPDATE eh_metadata SET removed=? WHERE gid=?", (0 if undo else 1, gid))
        conn.commit()
    return {"ok": True, "removed": not undo}


def merge_items(ids: list[str]) -> dict:
    """手动合并：把多条推荐（同一部作品的不同站条目）合并成一条，链接归并。"""
    global _LAST_MERGE
    if not ids or len(ids) < 2:
        return {"ok": False, "error": "至少需要两条"}
    with get_conn() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                f"SELECT * FROM recommended WHERE work_id IN ({','.join('?' * len(ids))})", ids
            )
        ]
    if len(rows) < 2:
        return {"ok": False, "error": "条目不存在（可能已被合并）"}
    primary = max(rows, key=lambda r: (r["score"] or 0))
    # 备份以便撤回
    _LAST_MERGE = {
        "rows": rows,
        "primary_id": primary["work_id"],
        "time": time.time(),
    }
    links = []
    seen = set()
    for r in rows:
        for l in json.loads(r["links"] or "[]"):
            k = l.get("url")
            if k and k not in seen:
                seen.add(k)
                links.append(l)
    feedback = "like" if any(r["feedback"] == "like" for r in rows) else None
    cover = primary.get("cover_url") or next((r["cover_url"] for r in rows if r.get("cover_url")), "")
    rating = primary.get("rating") or next((r["rating"] for r in rows if r.get("rating")), 0)
    rating_count = primary.get("rating_count") or next(
        (r["rating_count"] for r in rows if r.get("rating_count")), 0
    )
    pages = primary.get("pages") or next((r["pages"] for r in rows if r.get("pages")), 0)
    with get_conn() as conn:
        for r in rows:
            if r["work_id"] != primary["work_id"]:
                conn.execute("DELETE FROM recommended WHERE work_id=?", (r["work_id"],))
        conn.execute(
            """UPDATE recommended SET links=?, feedback=?, cover_url=?, rating=?, rating_count=?, pages=?
               WHERE work_id=?""",
            (json.dumps(links, ensure_ascii=False), feedback, cover, rating, rating_count,
             pages, primary["work_id"]),
        )
        conn.commit()
    return {"ok": True, "merged": len(rows), "work_id": primary["work_id"]}


_LAST_MERGE: dict | None = None


def unmerge() -> dict:
    """撤回最近一次手动合并（60 秒内有效）。"""
    global _LAST_MERGE
    if not _LAST_MERGE or time.time() - _LAST_MERGE["time"] > 120:
        return {"ok": False, "error": "没有可撤回的合并（或已超时）"}
    backup = _LAST_MERGE
    _LAST_MERGE = None
    with get_conn() as conn:
        for r in backup["rows"]:
            conn.execute(
                """INSERT OR REPLACE INTO recommended
                   (work_id, primary_site, title, author, tags, rating, rating_count, pages,
                    lang, channel, score, reason, links, first_seen, cover_url, feedback,
                    feedback_at, feedback_applied)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    r["work_id"], r["primary_site"], r["title"], r["author"], r["tags"],
                    r["rating"], r["rating_count"], r["pages"], r["lang"], r["channel"],
                    r["score"], r["reason"], r["links"], r["first_seen"], r["cover_url"],
                    r["feedback"], r["feedback_at"], r["feedback_applied"],
                ),
            )
        conn.commit()
    return {"ok": True, "restored": len(backup["rows"])}


def _handle_feedback(body: dict) -> dict:
    work_id = body.get("work_id")
    choice = body.get("choice")
    if choice not in ("like", "meh", "hide", "undo") or not work_id:
        return {"ok": False, "error": "参数错误"}
    if choice == "undo":
        with get_conn() as conn:
            row = conn.execute(
                "SELECT feedback, feedback_applied FROM recommended WHERE work_id=?", (work_id,)
            ).fetchone()
        if not row or not row[0]:
            return {"ok": False, "error": "没有可撤回的反馈"}
        if row[1]:
            # 已随更新推荐折算：回滚权重后再清除
            from .feedback import revert_feedback

            if revert_feedback(work_id):
                return {"ok": True, "undo": True, "reverted": True}
            return {"ok": False, "error": "回滚失败"}
        with get_conn() as conn:
            conn.execute(
                "UPDATE recommended SET feedback=NULL, feedback_at=NULL, feedback_applied=0 "
                "WHERE work_id=?",
                (work_id,),
            )
            conn.commit()
        return {"ok": True, "undo": True}
    if choice == "like":
        # 已收藏则反转为取消收藏（须在 record 之前检查）
        with get_conn() as conn:
            cur = conn.execute(
                "SELECT feedback FROM recommended WHERE work_id=?", (work_id,)
            ).fetchone()
        if cur and cur[0] == "like":
            with get_conn() as conn:
                conn.execute(
                    "UPDATE recommended SET feedback=NULL, feedback_at=NULL, feedback_applied=0 "
                    "WHERE work_id=?",
                    (work_id,),
                )
                conn.commit()
            _remove_liked(work_id)
            return {"ok": True, "unliked": True}
    rc = record(work_id, choice)
    if rc != 0:
        return {"ok": False, "error": "找不到该推荐条目"}
    with get_conn() as conn:
        row = dict(conn.execute(
            "SELECT * FROM recommended WHERE work_id=?", (work_id,)).fetchone())
    if choice == "like":
        # 收藏存档即可；权重折算推迟到下一次"更新推荐"时统一进行
        _add_liked(row)
    # meh / hide：仅记录，权重同样在"更新推荐"时统一折算
    return {"ok": True}


PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>漫画推荐 · 我的收藏画像</title>
<style>
:root{
  --bg:#eef0f6; --card:#ffffff; --card2:#f2f5fa;
  --line:#e3e7f0; --line2:#cdd5e3;
  --txt:#1c2333; --sub:#6d7689; --acc:#5b7cfa; --acc2:#8f6df0;
  --like:#12a35c; --meh:#e5533d; --gold:#d99a06;
  --glass:blur(20px) saturate(150%);
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{
  background:var(--bg); color:var(--txt);
  font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif;
  min-height:100vh; overflow-x:hidden;
}
/* 背景光斑（浅色） */
.bg-orb{position:fixed;border-radius:50%;filter:blur(90px);opacity:.30;pointer-events:none;z-index:0}
.o1{width:480px;height:480px;background:#b9c8ff;top:-160px;left:-120px}
.o2{width:420px;height:420px;background:#e3c7ff;bottom:-140px;right:-100px}
.o3{width:300px;height:300px;background:#bfe7f2;top:40%;left:60%}
/* 书架背景（收藏封面竖列，纯装饰） */
.bg-shelf{position:fixed;inset:0;z-index:0;pointer-events:none;display:flex;flex-direction:column;
  justify-content:center;gap:36px;padding:70px 4vw;filter:blur(7px) saturate(.9) brightness(1.02);opacity:.85;
  -webkit-mask-image:radial-gradient(ellipse at center,#000 30%,transparent 80%);
  mask-image:radial-gradient(ellipse at center,#000 30%,transparent 80%)}
.shelf-row{display:flex;justify-content:center;gap:10px}
.shelf-row:nth-child(odd){transform:rotate(1.2deg)}
.shelf-row:nth-child(even){transform:rotate(-1.2deg)}
.spine{position:relative;width:48px;height:164px;border-radius:5px 8px 8px 5px;background-size:cover;background-position:center;
  box-shadow:0 8px 22px rgba(30,40,70,.22);border:1px solid rgba(255,255,255,.65)}
.spine::after{content:"";position:absolute;top:0;bottom:0;left:4px;width:3px;background:rgba(30,40,70,.16)}
.bg-veil{position:fixed;inset:0;z-index:0;pointer-events:none;
  background:linear-gradient(rgba(240,243,250,.42),rgba(240,243,250,.55))}
main{position:relative;z-index:1;padding:18px 26px 90px;margin-left:216px;transition:margin-left .28s ease}
.content-wrap{max-width:1280px;margin:0 auto}
/* 左侧竖列导航（可收缩） */
.sidebar{
  position:fixed;left:0;top:0;bottom:0;z-index:60;width:216px;
  display:flex;flex-direction:column;padding:16px 12px 18px;
  background:rgba(255,255,255,.78);backdrop-filter:var(--glass);
  border-right:1px solid var(--line);box-shadow:4px 0 30px rgba(40,55,95,.06);
  transition:width .28s ease;
}
.sb-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;padding:0 6px}
.logo{font-size:17px;font-weight:800;letter-spacing:.5px;white-space:nowrap;color:var(--txt)}
.logo span{background:linear-gradient(90deg,#4f6ef7,#9a6df5);-webkit-background-clip:text;background-clip:text;color:transparent}
.collapse-btn{width:28px;height:28px;border-radius:9px;border:1px solid var(--line);background:var(--card);
  color:var(--sub);cursor:pointer;font-size:13px;transition:.2s;flex:none}
.collapse-btn::before{content:'«'}
body.sb-collapsed .collapse-btn::before{content:'»'}
.collapse-btn:hover{border-color:var(--acc);color:var(--acc)}
.tabs{display:flex;flex-direction:column;gap:5px;flex:1}
.tab{
  display:flex;align-items:center;gap:11px;padding:10px 12px;border-radius:12px;cursor:pointer;
  font-size:13.5px;color:var(--sub);border:1px solid transparent;transition:.2s;
  white-space:nowrap;user-select:none;overflow:hidden;
}
.tab:hover{color:var(--txt);background:var(--card2)}
.tab.active{
  color:#fff;background:linear-gradient(135deg,#5b7cfa,#8f6df0);
  border-color:transparent;box-shadow:0 5px 16px rgba(91,124,250,.32);
}
.nav-ico{width:20px;text-align:center;flex:none;font-size:15px}
.nav-label{transition:opacity .2s;opacity:1}
.sb-bottom{display:flex;flex-direction:column;gap:8px;padding-top:12px;border-top:1px solid var(--line)}
.sb-stats{font-size:11.5px;color:var(--sub);line-height:1.85;padding:8px 12px;
  background:var(--card);border:1px solid var(--line);border-radius:12px;white-space:nowrap;overflow:hidden}
.sb-stats b{color:var(--txt)}
.sb-btn{display:flex;align-items:center;gap:8px;justify-content:flex-start;text-align:left;overflow:hidden}
.site-sel{
  width:100%;background:var(--card);color:var(--txt);border:1px solid var(--line);border-radius:10px;
  padding:6px 10px;font-size:12.5px;outline:none;cursor:pointer;
}
.chip{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:4px 12px;font-size:12px;color:var(--sub)}
.chip b{color:var(--txt)}
button.chip{cursor:pointer}
button.chip:hover{border-color:var(--line2)}
/* 收缩态 */
body.sb-collapsed .sidebar{width:68px;padding:16px 8px 18px}
body.sb-collapsed .nav-label{opacity:0;width:0;display:none}
body.sb-collapsed .logo span{display:none}
body.sb-collapsed .sb-head{justify-content:center}
body.sb-collapsed .tab{justify-content:center;padding:10px 0}
body.sb-collapsed .sb-stats{display:none}
body.sb-collapsed .site-sel{display:none}
body.sb-collapsed .sb-btn{justify-content:center}
body.sb-collapsed main{margin-left:68px}
/* 视图 */
.view{display:none;animation:fadein .35s ease}
.view.active{display:block}
@keyframes fadein{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
h2.sec{font-size:17px;margin:26px 0 14px;display:flex;align-items:center;gap:8px;color:var(--txt)}
h2.sec::before{content:"";width:5px;height:18px;border-radius:3px;background:linear-gradient(180deg,var(--acc),var(--acc2))}
/* 首页 hero */
.hero{text-align:center;padding:44px 0 6px}
.hero h1{font-size:34px;font-weight:900;letter-spacing:1px;color:var(--txt)}
.hero h1 em{font-style:normal;background:linear-gradient(90deg,#4f6ef7,#9a6df5,#e879a5);-webkit-background-clip:text;background-clip:text;color:transparent}
.hero .sub{color:var(--sub);font-size:14px;margin-top:12px}
.hero .chips{display:flex;justify-content:center;gap:8px;flex-wrap:wrap;margin-top:16px}
/* 首页 双列斜向滚动（封面带） */
.marquee-zone{position:relative;margin:30px 0 10px;padding:30px 0}
.marquee-band{
  transform:rotate(-3deg) scale(1.04);overflow:hidden;margin:-4px 0;
  -webkit-mask-image:linear-gradient(90deg,transparent,#000 6%,#000 94%,transparent);
  mask-image:linear-gradient(90deg,transparent,#000 6%,#000 94%,transparent);
}
.marquee-track{display:flex;gap:26px;width:max-content;padding:12px 0}
.marquee-band .marquee-track{animation:slide 55s linear infinite}
.marquee-band.rev .marquee-track{animation:slide-rev 65s linear infinite}
.marquee-band:hover .marquee-track{animation-play-state:paused}
@keyframes slide{from{transform:translateX(0)}to{transform:translateX(-50%)}}
@keyframes slide-rev{from{transform:translateX(-50%)}to{transform:translateX(0)}}
.mq-item{position:relative;flex:none;width:264px;cursor:pointer}
.mq-item img{width:264px;height:370px;object-fit:cover;border-radius:16px;background:#e8ebf3;
  border:1px solid rgba(25,36,62,.16);box-shadow:0 16px 42px rgba(35,48,84,.26);transition:.38s cubic-bezier(.2,.8,.3,1.2)}
.mq-item img:hover{transform:scale(1.06) translateY(-8px);border-color:#9db4f5;
  box-shadow:0 24px 55px rgba(35,48,84,.32),0 0 30px rgba(91,124,250,.28)}
.mq-title{margin-top:9px;font-size:13px;color:#3d4659;line-height:1.4;font-weight:600;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;text-align:center}
.mq-author{font-size:11.5px;color:var(--sub);text-align:center;margin-top:3px}
/* 通用卡片网格（频道页） */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(215px,1fr));gap:16px}
.card{
  position:relative;border-radius:16px;overflow:hidden;cursor:pointer;
  background:var(--card);border:1px solid var(--line);
  transition:.28s cubic-bezier(.2,.8,.3,1.2);box-shadow:0 4px 16px rgba(40,55,95,.06);
}
.card:hover{transform:translateY(-5px);border-color:var(--line2);box-shadow:0 16px 36px rgba(40,55,95,.14)}
.card .cover{width:100%;aspect-ratio:3/4;object-fit:cover;display:block;background:#e8ebf3}
.card .body{padding:10px 12px 12px}
.card .title{font-size:13.5px;font-weight:650;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:37px;color:var(--txt)}
.card .meta{display:flex;flex-wrap:wrap;gap:5px;margin-top:7px}
.badge{font-size:11px;padding:2px 8px;border-radius:7px;background:var(--card2);border:1px solid var(--line);color:var(--sub)}
.badge.zh{color:#b07d06;border-color:#f0d98c;background:#fdf6e0}
.badge.r{color:#0e7490;background:#e7f5f9;border-color:#c9e7ef}
.card .acts{display:flex;gap:8px;margin-top:10px}
.btn{flex:1;display:flex;align-items:center;justify-content:center;gap:5px;padding:7px 0;border-radius:10px;
  border:1px solid var(--line);background:var(--card);color:var(--sub);font-size:12.5px;cursor:pointer;transition:.18s}
.btn.like:hover{color:var(--like);border-color:var(--like);background:rgba(18,163,92,.09)}
.btn.meh:hover{color:var(--meh);border-color:var(--meh);background:rgba(229,83,61,.08)}
/* 已收藏状态 */
.card.liked{border:2px solid transparent;
  background:linear-gradient(#fff,#fff) padding-box,
             linear-gradient(135deg,rgba(18,163,92,.85),rgba(34,162,226,.8),rgba(217,154,6,.75)) border-box;
  box-shadow:0 0 20px rgba(18,163,92,.20)}
.card.liked:hover{box-shadow:0 14px 34px rgba(40,55,95,.12),0 0 26px rgba(18,163,92,.25)}
.btn.like.on{color:#fff;background:linear-gradient(135deg,#12a35c,#22a3e2);border-color:transparent;font-weight:700}
.heart-bounce{animation:hb .6s cubic-bezier(.3,2,.5,1)}
@keyframes hb{0%{transform:scale(1)}35%{transform:scale(1.55)}65%{transform:scale(.9)}100%{transform:scale(1)}}
/* 合并模式 */
.card.picked{outline:3px solid var(--gold);outline-offset:2px;box-shadow:0 0 26px rgba(217,154,6,.35)}
#merge-toggle.active{background:linear-gradient(135deg,#fdf3d7,#fde8c4);border-color:rgba(217,154,6,.6);color:#8a6400}
/* 收藏列表 */
.lib-filter{width:100%;max-width:340px;padding:10px 14px;border-radius:12px;margin:4px 0 18px;
  background:var(--card);border:1px solid var(--line);color:var(--txt);font-size:13.5px;outline:none}
.author-sec{margin-bottom:26px}
.author-head{display:flex;align-items:baseline;gap:10px;margin-bottom:12px;cursor:pointer;user-select:none}
.author-head .nm{font-size:16px;font-weight:750;color:var(--txt)}
.author-head .cnt{font-size:12px;color:var(--sub);border:1px solid var(--line);border-radius:20px;padding:2px 10px}
.lib-row{display:flex;gap:12px;overflow-x:auto;padding:6px 2px 14px;scrollbar-width:thin}
.lib-item{flex:none;width:118px;cursor:pointer;transition:.25s}
.lib-item img{width:118px;height:166px;object-fit:cover;border-radius:11px;border:1px solid var(--line)}
.lib-item:hover{transform:translateY(-4px)}
.lib-item:hover img{border-color:#9db4f5;box-shadow:0 10px 22px rgba(40,55,95,.18)}
.lib-item .t{font-size:11.5px;color:var(--sub);margin-top:6px;line-height:1.35;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
/* 弹窗（浅色） */
.modal-mask{position:fixed;inset:0;z-index:100;display:none;align-items:center;justify-content:center;
  background:rgba(20,26,44,.45);backdrop-filter:blur(8px);padding:24px}
.modal-mask.open{display:flex;animation:fadein .25s ease}
.modal{
  width:min(880px,96vw);max-height:90vh;overflow:auto;border-radius:22px;padding:24px;
  background:#fdfdfb;color:#242a37;border:1px solid #e7e9f0;
  box-shadow:0 30px 80px rgba(18,24,40,.30);display:grid;grid-template-columns:300px 1fr;gap:22px;
  animation:pop .3s cubic-bezier(.2,1,.3,1.2);
}
@keyframes pop{from{opacity:0;transform:scale(.92) translateY(14px)}to{opacity:1;transform:none}}
.modal .m-cover{width:300px;border-radius:14px;border:1px solid #e7e9f0;box-shadow:0 16px 40px rgba(30,40,70,.18)}
.modal .m-title{font-size:18px;font-weight:750;line-height:1.45;margin-bottom:12px;color:#161b27}
.modal .m-meta{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
.linkbtn{display:inline-flex;align-items:center;gap:6px;padding:8px 14px;margin:0 8px 8px 0;border-radius:11px;
  background:#edf2ff;border:1px solid #c9d6f5;color:#2f4f9f;font-size:13px;text-decoration:none;transition:.2s}
.linkbtn:hover{transform:translateY(-2px);box-shadow:0 8px 20px rgba(91,124,250,.25)}
.tagchip{display:inline-block;margin:0 6px 6px 0;padding:4px 11px;border-radius:20px;font-size:12px;cursor:pointer;
  background:#eef1f7;border:1px solid #dde2ec;color:#4a5468;transition:.18s}
.tagchip:hover{border-color:#6c8cff;background:#e6ecff;color:#24304e}
.tagchip.author{background:#fdf3d7;border-color:#f0d98c;color:#8a6400}
.reason{margin:14px 0;padding:12px 14px;border-radius:12px;background:#f3f5fa;
  border-left:3px solid #6c8cff;color:#4a5468;font-size:13.5px;line-height:1.7}
.desc{margin:0 0 12px;padding:12px 14px;border-radius:12px;background:#f8fafd;
  border:1px solid #e4e9f2;color:#3d4659;font-size:13px;line-height:1.75;white-space:pre-line}
.m-actions{display:flex;gap:10px;margin-top:16px}
.m-actions .btn{padding:10px 0;font-size:13.5px}
/* 粒子 */
.particle{position:fixed;width:9px;height:9px;border-radius:50%;pointer-events:none;z-index:200;
  animation:pfly 1.35s cubic-bezier(.2,.6,.3,1) forwards}
@keyframes pfly{0%{transform:translate(0,0) scale(1);opacity:1}
  60%{opacity:.9}
  100%{transform:translate(var(--dx),var(--dy)) scale(0);opacity:0}}
.card.removing{transition:.9s;transform:scale(.9) rotate(1.5deg);opacity:0}
.empty{color:var(--sub);text-align:center;padding:60px 20px;font-size:14px}
.mq-empty{opacity:.7}
/* 卡片右上角忽略按钮 */
.card .dismiss{position:absolute;top:8px;right:8px;z-index:5;width:26px;height:26px;border-radius:50%;
  display:flex;align-items:center;justify-content:center;background:rgba(255,255,255,.75);backdrop-filter:blur(8px);
  border:1px solid var(--line);color:#7b8497;font-size:13px;cursor:pointer;opacity:0;transition:.2s}
.card:hover .dismiss{opacity:1}
.card .dismiss:hover{color:#fff;background:rgba(229,83,61,.8);border-color:var(--meh)}
/* 更新推荐进度面板（浅色，左下） */
.refresh-panel{position:fixed;left:22px;bottom:22px;z-index:150;display:none;width:320px;max-width:92vw;
  background:rgba(255,255,255,.92);backdrop-filter:var(--glass);border:1px solid var(--line2);
  border-radius:16px;padding:14px 16px;box-shadow:0 18px 50px rgba(40,55,95,.22)}
.rp-title{font-size:13px;font-weight:700;margin-bottom:8px;color:var(--txt)}
.rp-log{font-size:11px;color:var(--sub);line-height:1.7;max-height:150px;overflow:hidden}
/* 撤回面板（右下） */
.undo-panel{position:fixed;right:22px;bottom:22px;z-index:160;display:none;align-items:center;gap:12px;
  background:rgba(255,255,255,.94);backdrop-filter:var(--glass);border:1px solid var(--line2);
  border-radius:14px;padding:10px 14px;box-shadow:0 18px 50px rgba(40,55,95,.24);animation:pop .25s ease}
.undo-panel.show{display:flex}
.up-text{font-size:12.5px;color:var(--sub);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.up-btn{flex:none;padding:6px 14px;border-radius:9px;border:1px solid var(--meh);color:var(--meh);
  background:#fdf2f0;font-size:12px;cursor:pointer;font-weight:700;transition:.18s}
.up-btn:hover{background:var(--meh);color:#fff}
/* 选站弹窗 */
.site-modal{width:min(400px,92vw);display:block}
.site-list{display:flex;flex-direction:column;gap:10px;margin:14px 0}
.site-opt{display:flex;align-items:center;gap:10px;padding:11px 14px;border-radius:11px;
  background:var(--card);border:1px solid var(--line);font-size:14px;color:var(--txt);cursor:pointer;transition:.15s}
.site-opt:hover{border-color:var(--acc)}
.site-opt.disabled{opacity:.5;cursor:not-allowed}
.site-opt input{width:16px;height:16px;accent-color:#5b7cfa}
.site-opt em{font-style:normal;font-size:12px;color:var(--meh)}
/* 跨站搜索 */
.search-bar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
.search-bar input[type=text],.search-bar input:not([type]){flex:1;min-width:260px;padding:11px 16px;border-radius:12px;
  background:var(--card);border:1px solid var(--line);color:var(--txt);font-size:14px;outline:none}
.search-bar input:focus{border-color:var(--acc);box-shadow:0 0 0 3px rgba(91,124,250,.15)}
.search-author{font-size:13px;color:var(--sub);display:flex;align-items:center;gap:6px;cursor:pointer;user-select:none}
.search-go{padding:10px 26px;border-radius:12px;border:none;cursor:pointer;font-size:14px;font-weight:700;color:#fff;
  background:linear-gradient(135deg,#5b7cfa,#8f6df0);box-shadow:0 6px 18px rgba(91,124,250,.3);transition:.2s}
.search-go:hover{transform:translateY(-1px)}
.search-go:disabled{opacity:.6;cursor:wait}
.search-status{color:var(--sub);font-size:13px;margin:6px 0 16px}
.sr-group{margin-bottom:24px}
.sr-site{font-size:14px;font-weight:750;color:var(--txt);margin-bottom:10px;display:flex;align-items:center;gap:8px}
.sr-site .cnt{font-size:11.5px;color:var(--sub);border:1px solid var(--line);border-radius:20px;padding:1px 9px}
.sr-row{display:flex;align-items:center;gap:12px;padding:9px 12px;border-radius:12px;margin-bottom:8px;
  background:var(--card);border:1px solid var(--line);transition:.2s}
.sr-row:hover{border-color:var(--acc);box-shadow:0 6px 18px rgba(40,55,95,.10)}
.sr-row img{width:56px;height:78px;object-fit:cover;border-radius:7px;border:1px solid var(--line);flex:none;background:#e8ebf3}
.sr-row .sr-info{flex:1;min-width:0}
.sr-row .sr-title{font-size:13.5px;font-weight:650;color:var(--txt);line-height:1.4;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.sr-row .sr-author{font-size:12px;color:var(--sub);margin-top:3px}
.sr-row a.sr-link{flex:none;padding:7px 14px;border-radius:9px;background:#edf2ff;border:1px solid #c9d6f5;
  color:#2f4f9f;font-size:12.5px;text-decoration:none;transition:.18s}
.sr-row a.sr-link:hover{background:#dde8ff}
.sr-row button.sr-fav{flex:none;padding:7px 12px;border-radius:9px;background:#fff;border:1px solid var(--line2);
  color:var(--sub);font-size:12.5px;cursor:pointer;transition:.18s}
.sr-row button.sr-fav:hover{border-color:var(--like);color:var(--like)}
.sr-row button.sr-fav.on{background:#e7f7ee;border-color:#9fd9b9;color:#0e8a4e}
@media (max-width:760px){
  .modal{grid-template-columns:1fr}
  .modal .m-cover{width:150px;margin:0 auto}
  .hero h1{font-size:25px}
  .mq-item{width:180px}
  .mq-item img{width:180px;height:252px}
  /* 小屏侧边栏默认收缩 */
  .sidebar{width:68px;padding:16px 8px 18px}
  .nav-label{display:none}
  .logo span{display:none}
  .sb-head{justify-content:center}
  .tab{justify-content:center;padding:10px 0}
  .sb-stats{display:none}
  .site-sel{display:none}
  .sb-btn{justify-content:center}
  main{margin-left:68px}
}
</style>

</head>
<body>
<div class="bg-shelf" id="bg-shelf"></div>
<div class="bg-veil"></div>
<div class="bg-orb o1"></div><div class="bg-orb o2"></div><div class="bg-orb o3"></div>
<main>
  <aside class="sidebar" id="sidebar">
    <div class="sb-head">
      <div class="logo">🎴 <span>漫画推荐</span></div>
      <button class="collapse-btn" id="collapse-btn" title="收起 / 展开"></button>
    </div>
    <div class="tabs" id="tabs">
      <div class="tab active" data-view="home"><span class="nav-ico">🏠</span><span class="nav-label">首页</span></div>
      <div class="tab" data-view="precision"><span class="nav-ico">🎯</span><span class="nav-label">精准推荐</span></div>
      <div class="tab" data-view="trending"><span class="nav-ico">🔥</span><span class="nav-label">热作排行</span></div>
      <div class="tab" data-view="series"><span class="nav-ico">📚</span><span class="nav-label">连载追踪</span></div>
      <div class="tab" data-view="library"><span class="nav-ico">❤️</span><span class="nav-label">收藏列表</span></div>
      <div class="tab" data-view="search"><span class="nav-ico">🔍</span><span class="nav-label">搜索</span></div>
    </div>
    <div class="sb-bottom">
      <div class="sb-stats" id="status-chip">加载中…</div>
      <button class="chip sb-btn" id="refresh-btn" title="生成一批新推荐（约 20-40 分钟）">🔄 <span class="nav-label">更新推荐</span></button>
      <button class="chip sb-btn" id="merge-toggle" title="合并同一部作品的不同站点条目">⤴ <span class="nav-label">合并模式</span></button>
      <button class="chip sb-btn" id="data-dir-btn" title="打开数据目录（config.json / cookie / 数据库）" onclick="openDataDir()">📂 <span class="nav-label">数据目录</span></button>
      <select class="site-sel" id="site-sel" title="点作者/标签时用哪个站搜索"></select>
    </div>
  </aside>
  <div class="content-wrap">

  <div class="view active" id="view-home">
    <div class="hero">
      <h1>为你找到下一本 <em>对胃口</em> 的好漫画</h1>
      <div class="sub" id="hero-sub"></div>
      <div class="chips" id="hero-chips"></div>
    </div>
    <div class="marquee-zone" id="marquee-zone"></div>
  </div>

  <div class="view" id="view-precision"><h2 class="sec">精准推荐 · 命中你的画像</h2><div class="grid" id="grid-precision"></div></div>
  <div class="view" id="view-trending"><h2 class="sec">热作排行 · 热门 × 画像过滤</h2><div class="grid" id="grid-trending"></div></div>
  <div class="view" id="view-series"><h2 class="sec">连载追踪 · 你收藏系列的未收卷</h2><div class="grid" id="grid-series"></div></div>
  <div class="view" id="view-library">
    <h2 class="sec">我的收藏 · 按作者分组（作者别名已合并）</h2>
    <input class="lib-filter" id="lib-filter" placeholder="🔍 筛选作者或作品…">
    <div id="lib-body"></div>
  </div>
  <div class="view" id="view-search">
    <h2 class="sec">跨站搜索 · 名字 / 作者名</h2>
    <div class="search-bar">
      <input id="search-input" placeholder="输入中文名、日文名、罗马音或作者名，回车搜索" onkeydown="if(event.key==='Enter')doSearch()">
      <label class="search-author"><input type="checkbox" id="search-author"> 按作者名</label>
      <button class="search-go" id="search-go" onclick="doSearch()">搜索</button>
    </div>
    <div class="search-status" id="search-status"></div>
    <div id="search-results"></div>
  </div>
  </div>
</main>

<div class="modal-mask" id="modal-mask" onclick="if(event.target===this)closeModal()">
  <div class="modal" id="modal"></div>
</div>

<div class="refresh-panel" id="refresh-panel">
  <div class="rp-title" id="refresh-title">⏳ 正在生成推荐…</div>
  <div class="rp-log" id="refresh-log"></div>
</div>

<div class="undo-panel" id="undo-panel">
  <span class="up-text" id="undo-text">已屏蔽</span>
  <button class="btn up-btn" id="undo-btn" onclick="undoAction()">撤销 (5s)</button>
</div>

<div class="modal-mask" id="site-mask" onclick="if(event.target===this)closeSiteModal()">
  <div class="modal site-modal">
    <div class="m-title">选择本次推荐使用的站点</div>
    <div class="site-list" id="site-list"></div>
    <div class="m-actions">
      <button class="btn" onclick="closeSiteModal()">取消</button>
      <button class="btn like on" onclick="confirmRefresh()">开始更新</button>
    </div>
  </div>
</div>

<script>
const esc = s => (s||'').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const CH = {precision:'精准推荐', trending:'热作推荐', series:'连载追踪'};
let ITEMS=[], LIB={groups:[]}, STATUS={}, CFG={search_site:'ehentai', options:{}};
let CURRENT_VIEW = 'home';
const $ = id => document.getElementById(id);

const SEARCH_URLS = {
  ehentai: q => 'https://e-hentai.org/?f_search=' + encodeURIComponent(q) + '&f_cats=0',
  nhentai: q => 'https://nhentai.net/search/?q=' + encodeURIComponent(q),
  wnacg:   q => 'https://www.wnacg.com/search/?q=' + encodeURIComponent(q),
  '18comic': q => 'https://18comic.vip/search/photos?search_query=' + encodeURIComponent(q),
};
function searchUrl(q){ const f = SEARCH_URLS[CFG.search_site] || SEARCH_URLS.ehentai; return f(q); }
function tagSearch(el){
  // 点中文标签：E-Hentai/nHentai/绅士漫画 用原文搜索，禁漫 用中文搜索
  const orig = el.dataset.orig || '', disp = el.dataset.display || orig;
  const q = (CFG.search_site === '18comic') ? disp : orig;
  window.open(searchUrl(q), '_blank');
}

async function load(){
  ITEMS = await (await fetch('/api/items')).json();
  LIB = await (await fetch('/api/library')).json();
  STATUS = await (await fetch('/api/status')).json();
  CFG = await (await fetch('/api/config')).json();
  // 站点选择器
  const sel = $('site-sel');
  sel.innerHTML = Object.entries(CFG.options||{}).map(([k,v]) =>
    `<option value="${esc(k)}" ${k===CFG.search_site?'selected':''}>搜索站：${esc(v.label)}</option>`).join('');
  sel.onchange = async () => {
    await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({search_site: sel.value})});
    CFG.search_site = sel.value;
    toast('已切换搜索站：' + (CFG.options[sel.value]?.label || sel.value));
  };
  // 状态条
  $('status-chip').innerHTML =
    `画像 <b>${STATUS.total_works}</b> 部<br>收藏 <b>${STATUS.n_fav ?? STATUS.n_library}</b> 部<br>平均 <b>${STATUS.mean_rating ?? '-'}</b> ★<br>推荐 <b>${STATUS.n_rec}</b> 条<br>屏蔽 <b>${STATUS.n_meh}</b> · 忽略 <b>${STATUS.n_hide ?? 0}</b>`;
  if(STATUS.data_dir) $('data-dir-btn').title = '数据目录：' + STATUS.data_dir + '（config.json 在此，AI key 与代理配置；cookie 文件放 data 子目录）';
  $('hero-sub').textContent =
    `基于 ${STATUS.total_works} 部收藏的画像 · 中文版占比 ${(STATUS.zh_ratio*100).toFixed(0)}% · ` +
    `库内 ${LIB.total} 部作品 · ${LIB.author_count} 位作者`;
  $('hero-chips').innerHTML =
    (STATUS.top_authors||[]).map(a=>`<span class="chip">✍ ${esc(a)}</span>`).join('') +
    (STATUS.top_tags||[]).map(t=>`<span class="chip"># ${esc(t)}</span>`).join('');
  renderBookshelf();
  renderHome();
  renderChannels();
  renderLibrary();
  setTab(CURRENT_VIEW);
}

/* ---------- 首页 ---------- */
function shuffle(arr){ const a=[...arr]; for(let i=a.length-1;i>0;i--){const j=Math.floor(Math.random()*(i+1));[a[i],a[j]]=[a[j],a[i]];} return a; }
// 封面图地址：hitomi 图床要求 hitomi.la Referer，浏览器直连必 404，改走服务端代理
function imgUrl(u){
  if(!u) return '';
  return /gold-usergeneratedcontent\.net/i.test(u) ? '/api/img?u='+encodeURIComponent(u) : esc(u);
}
function coverOf(it){ return it.cover_url ? imgUrl(it.cover_url) : ''; }
/* ---------- 首页：书架背景 + 3D 圆环 ---------- */
function renderBookshelf(){
  const covers = [];
  for(const g of (LIB.groups||[])) for(const it of g.items) if(it.cover) covers.push(it.cover);
  const picks = shuffle(covers).slice(0, 96);
  const rows = [];
  for(let s=0; s<4; s++){
    rows.push(`<div class="shelf-row">${picks.slice(s*24,(s+1)*24).map(c=>`<div class="spine" style="background-image:url('${imgUrl(c)}')"></div>`).join('')}</div>`);
  }
  $('bg-shelf').innerHTML = rows.join('');
}
function renderHome(){
  // 首页主体：两行反向大封面斜向滚动带（随机挑选，刷新重随机；悬停暂停、点击弹详情）
  const zone = $('marquee-zone');
  if(!ITEMS.length){ zone.innerHTML='<div class="empty mq-empty">还没有推荐，先点右上角「🔄 更新推荐」生成一批</div>'; return; }
  const row1 = shuffle(ITEMS).slice(0, 12), row2 = shuffle(ITEMS).slice(0, 12);
  const band = items => items.map(it =>
    `<div class="mq-item" onclick="openModalById('${esc(it.work_id)}')">` +
    (it.cover_url?`<img loading="lazy" referrerpolicy="no-referrer" src="${coverOf(it)}" onerror="this.parentElement.style.visibility='hidden'">`:`<div style="width:264px;height:370px;border-radius:16px;background:#e8ebf3"></div>`) +
    `<div class="mq-title">${esc(it.title)}</div>` +
    (it.author?`<div class="mq-author">✍ ${esc(it.author)}</div>`:'') +
    `</div>`).join('');
  zone.innerHTML =
    `<div class="marquee-band"><div class="marquee-track">${band(row1)}${band(row1)}</div></div>` +
    `<div class="marquee-band rev"><div class="marquee-track">${band(row2)}${band(row2)}</div></div>`;
}

/* ---------- 频道页 ---------- */
function cardHTML(it){
  const badges = [];
  if(it.author) badges.push(`<span class="badge">✍ ${esc(it.author)}</span>`);
  if(it.rating) badges.push(`<span class="badge r">★ ${it.rating}</span>`);
  if(it.pages) badges.push(`<span class="badge">${it.pages}页</span>`);
  if(it.is_chinese) badges.push(`<span class="badge zh">中文</span>`);
  return `<div class="card ${it.liked?'liked':''} ${it.work_id===MERGE_PICK?'picked':''}" id="card-${esc(it.work_id).replace(/[^a-zA-Z0-9_-]/g,'_')}" onclick="cardClick('${esc(it.work_id)}')">` +
    `<div class="dismiss" title="忽略此条（不影响推荐权重）" onclick="event.stopPropagation();vote('${esc(it.work_id)}','hide')">✕</div>` +
    (it.cover_url?`<img class="cover" loading="lazy" referrerpolicy="no-referrer" src="${coverOf(it)}" onerror="this.style.visibility='hidden'">`:`<div class="cover"></div>`) +
    `<div class="body">
      <div class="title">${esc(it.title)}</div>
      <div class="meta">${badges.join('')}</div>
      <div class="acts" onclick="event.stopPropagation()">
        <button class="btn like ${it.liked?'on':''}" id="lk-${esc(it.work_id).replace(/[^a-zA-Z0-9_-]/g,'_')}" onclick="vote('${esc(it.work_id)}','like')">${it.liked?'❤ 已收藏':'🤍 收藏'}</button>
        <button class="btn meh" onclick="vote('${esc(it.work_id)}','meh')">✕ 不感兴趣</button>
      </div>
    </div></div>`;
}
function renderChannels(){
  for(const ch of ['precision','trending','series']){
    const list = ITEMS.filter(it=>it.channel===ch);
    const el = $('grid-'+ch);
    el.innerHTML = list.length ? list.map(cardHTML).join('') : '<div class="empty">该频道暂无内容</div>';
  }
}

/* ---------- 收藏列表 ---------- */
function renderLibrary(filter){
  if(filter === undefined){ const f = $('lib-filter'); filter = f ? f.value : ''; }
  const ft = (filter||'').trim().toLowerCase();
  const groups = (LIB.groups||[]).filter(g =>
    !ft || g.author.toLowerCase().includes(ft) || (g.items||[]).some(i=>i.title.toLowerCase().includes(ft)));
  $('lib-body').innerHTML = groups.length ? groups.map(g => `
    <div class="author-sec">
      <div class="author-head">
        <span class="nm">${esc(g.author)}</span>
        <span class="cnt">${g.count} 部</span>
      </div>
      <div class="lib-row">${g.items.map((i,idx) => `
        <div class="lib-item" onclick="openLibModal('${esc(g.author)}',${idx})">
          ${i.cover?`<img loading="lazy" referrerpolicy="no-referrer" src="${imgUrl(i.cover)}" onerror="this.style.visibility='hidden'">`:''}
          <div class="t">${esc(i.title)}</div>
          ${i.fav?'<div style="font-size:10px;color:#34d399;margin-top:2px">❤ 手点收藏</div>':''}
        </div>`).join('')}
      </div>
    </div>`).join('') : '<div class="empty">没有匹配的作者或作品</div>';
}
$('lib-filter').addEventListener('input', e=>renderLibrary(e.target.value));

/* ---------- 弹窗 ---------- */
let LIB_INDEX = {};
let CUR_LIB = null;
function openModalById(workId){
  const it = ITEMS.find(x=>x.work_id===workId);
  if(it) openModal(it);
}
function openLibModal(author, idx){
  const g = (LIB.groups||[]).find(x=>x.author===author);
  const it = g && g.items[idx];
  if(!it) return;
  CUR_LIB = {fav: !!it.fav, work_id: it.work_id||'', gid: it.gid||'', title: it.title||''};
  openModal({...it, work_id:null, channel:'library', score:null,
    reason: it.reason || (it.fav?'你手点收藏的作品。':'这是你收藏库里的作品。'),
    links: it.links && it.links.length ? it.links : [{site:'e-hentai',url:it.url}],
    cover_url:it.cover, rating:it.rating, rating_count:it.rating_count, pages:it.pages,
    tags:it.tags, tags_pairs:it.tags_pairs, description:it.description, is_chinese:it.zh, liked:true});
}
function openModal(it){
  const main = (it.links&&it.links[0]&&it.links[0].url)||'#';
  const badges = [];
  if(it.author) badges.push(`<span class="badge">✍ ${esc(it.author)}</span>`);
  if(it.rating) badges.push(`<span class="badge r">★ ${it.rating}${it.rating_count?`（${it.rating_count} 人）`:''}</span>`);
  if(it.pages) badges.push(`<span class="badge">${it.pages} 页</span>`);
  if(it.is_chinese) badges.push(`<span class="badge zh">中文版</span>`);
  if(it.channel!=='library') badges.push(`<span class="badge">${CH[it.channel]||it.channel}</span>`);
  if(it.score!=null) badges.push(`<span class="badge">匹配 ${it.score}</span>`);
  const pairs = (it.tags_pairs||(it.tags||[]).map(t=>({orig:t,display:t}))).slice(0,14);
  const tags = pairs.map(p=>`<span class="tagchip" data-orig="${esc(p.orig)}" data-display="${esc(p.display)}" onclick="tagSearch(this)">${esc(p.display)}</span>`).join('');
  const links = (it.links||[]).map(l=>`<a class="linkbtn" href="${esc(l.url)}" target="_blank" rel="noreferrer">🔗 ${esc(l.site)}</a>`).join('');
  const isLib = it.channel==='library';
  $('modal').innerHTML = `
    ${it.cover_url?`<img class="m-cover" referrerpolicy="no-referrer" src="${imgUrl(it.cover_url)}" onerror="this.style.visibility='hidden'">`:'<div class="m-cover"></div>'}
    <div>
      <div class="m-title">${esc(it.title)}</div>
      ${it.description?`<div class="desc">${esc(it.description)}</div>`:''}
      <div class="m-meta">${badges.join('')}</div>
      ${it.author?`<div style="margin-bottom:10px"><span class="tagchip author" onclick="window.open(searchUrl('${esc(it.author.replace(/'/g,""))}'),'_blank')">✍ ${esc(it.author)} · 在站内搜索</span></div>`:''}
      ${tags?`<div style="margin-bottom:6px">${tags}</div>`:''}
      <div class="reason">${esc(it.reason||'')}</div>
      <div>${links}</div>
      ${!isLib?`<div class="m-actions">
        <button class="btn like ${it.liked?'on':''}" onclick="vote('${esc(it.work_id)}','like',true)">${it.liked?'❤ 已收藏':'🤍 收藏'}</button>
        <button class="btn meh" onclick="vote('${esc(it.work_id)}','meh',true)">✕ 不感兴趣</button>
        <button class="btn" onclick="vote('${esc(it.work_id)}','hide',true)">⊘ 忽略此条</button>
        <button class="btn" onclick="startMergeFromModal('${esc(it.work_id)}')">⤴ 合并到另一条</button>
      </div>`:(CUR_LIB&&CUR_LIB.fav
        ?`<div class="m-actions"><button class="btn meh" onclick="toggleLibFav()">💔 取消收藏</button></div>`
        :`<div class="m-actions"><button class="btn meh" onclick="unlib()">🗑 移出收藏库</button></div>`)}
    </div>`;
  $('modal-mask').classList.add('open');
}
function closeModal(){ $('modal-mask').classList.remove('open'); }

/* ---------- 反馈与动画 ---------- */
function toast(msg){
  let t = document.createElement('div');
  t.textContent = msg;
  t.style.cssText = 'position:fixed;bottom:28px;left:50%;transform:translateX(-50%);z-index:300;'+
    'background:rgba(255,255,255,.96);border:1px solid #d5dbe8;backdrop-filter:blur(12px);'+
    'padding:10px 20px;border-radius:14px;font-size:13px;color:#1c2333;'+
    'box-shadow:0 10px 30px rgba(40,55,95,.25);transition:.4s';
  document.body.appendChild(t);
  setTimeout(()=>{t.style.opacity='0'; setTimeout(()=>t.remove(),400)}, 2000);
}
function burst(card){
  const rect = card.getBoundingClientRect();
  const colors = ['#6c8cff','#c084fc','#f87171','#fbbf24','#34d399','#f0abfc','#8fe3ff','#f9a8d4'];
  const cx = rect.left + rect.width/2, cy = rect.top + rect.height/2;
  for(let i=0;i<32;i++){
    const p = document.createElement('div');
    p.className='particle';
    const ang = Math.random()*Math.PI*2, dist = 90+Math.random()*220;
    p.style.left=cx+'px'; p.style.top=cy+'px';
    p.style.background=colors[i%colors.length];
    p.style.setProperty('--dx', Math.cos(ang)*dist+'px');
    p.style.setProperty('--dy', (Math.sin(ang)*dist - 40)+'px');
    document.body.appendChild(p);
    setTimeout(()=>p.remove(), 1400);
  }
}
async function vote(workId, choice, fromModal=false){
  const r = await fetch('/api/feedback', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({work_id:workId, choice})});
  const d = await r.json();
  if(!d.ok){ toast(d.error||'操作失败'); return; }
  const it = ITEMS.find(x=>x.work_id===workId);
  const card = $('card-'+workId.replace(/[^a-zA-Z0-9_-]/g,'_'));
  if(choice==='meh'){
    if(card) burst(card);
    ITEMS = ITEMS.filter(x=>x.work_id!==workId);
    if(card){ card.classList.add('removing'); setTimeout(()=>{renderChannels();renderHome();}, 1150); }
    if(fromModal) closeModal();
    showUndoPanel('meh', workId, '已屏蔽《' + (it?it.title:'') + '》');
  } else if(choice==='hide'){
    ITEMS = ITEMS.filter(x=>x.work_id!==workId);
    if(card){ card.classList.add('removing'); setTimeout(()=>{renderChannels();renderHome();}, 300); }
    if(fromModal) closeModal();
    toast('已忽略此条（不影响推荐权重）');
  } else {
    if(d.unliked){
      // 取消收藏：同步卡片、收藏列表、书架
      if(it){ it.liked=false; }
      if(card){
        card.classList.remove('liked');
        const lk = $('lk-'+workId.replace(/[^a-zA-Z0-9_-]/g,'_'));
        if(lk){ lk.classList.remove('on'); lk.innerHTML='🤍 收藏'; }
      }
      try{
        LIB = await (await fetch('/api/library')).json();
        renderLibrary();
      }catch(e){}
      toast('已取消收藏');
      if(fromModal && it) setTimeout(()=>openModal(it), 60);
    } else {
      if(it){ it.liked=true; }
      if(card){
        card.classList.add('liked');
        const lk = $('lk-'+workId.replace(/[^a-zA-Z0-9_-]/g,'_'));
        if(lk){ lk.classList.add('on','heart-bounce'); lk.innerHTML='❤ 已收藏'; setTimeout(()=>lk.classList.remove('heart-bounce'),700); }
      }
      // 即时同步收藏列表（书架背景保持稳定，不随反馈重建）
      try{
        LIB = await (await fetch('/api/library')).json();
        renderLibrary();
      }catch(e){}
      toast('已收藏（将在更新推荐时计入画像）');
    }
  }
  const st = await (await fetch('/api/status')).json();
  $('status-chip').innerHTML = `画像 <b>${st.total_works}</b> 部<br>收藏 <b>${st.n_fav ?? st.n_library}</b> 部<br>平均 <b>${st.mean_rating ?? '-'}</b> ★<br>推荐 <b>${st.n_rec}</b> 条<br>屏蔽 <b>${st.n_meh}</b> · 忽略 <b>${st.n_hide ?? 0}</b>`;
  if(fromModal) setTimeout(()=>openModal(it), 60);
}

/* ---------- 合并模式 ---------- */
let MERGE_MODE = false, MERGE_PICK = null;
function cardClick(workId){
  if(MERGE_MODE){ mergePick(workId); return; }
  openModalById(workId);
}
function mergePick(workId){
  if(MERGE_PICK === null){
    MERGE_PICK = workId;
    renderChannels();
    toast('已选中第一条。再点另一条（同一部作品的其他站条目）完成合并');
  } else if(MERGE_PICK === workId){
    MERGE_PICK = null;
    renderChannels();
  } else {
    const a = MERGE_PICK, b = workId;
    MERGE_PICK = null;
    fetch('/api/merge', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ids:[a,b]})})
      .then(r=>r.json())
      .then(async d=>{
        if(d.ok){
          showUndoPanel('merge', a, `已合并 ${d.merged} 条 → 1 条`);
          await load();
        }
        else toast(d.error||'合并失败');
      });
  }
}
function toggleMerge(){
  MERGE_MODE = !MERGE_MODE;
  MERGE_PICK = null;
  $('merge-toggle').classList.toggle('active', MERGE_MODE);
  renderChannels();
  toast(MERGE_MODE ? '合并模式已开启：依次点击两条同作品卡片' : '合并模式已关闭');
}
$('merge-toggle').addEventListener('click', toggleMerge);
function startMergeFromModal(workId){
  closeModal();
  MERGE_MODE = true; MERGE_PICK = workId;
  $('merge-toggle').classList.add('active');
  renderChannels();
  toast('再点另一条（同一部作品的其他站条目）完成合并');
}

/* ---------- 更新推荐（选站弹窗） ---------- */
let REFRESH_TIMER = null;
$('refresh-btn').addEventListener('click', openSiteModal);
async function openSiteModal(){
  const d = await (await fetch('/api/sites')).json();
  const def = d.default || [];
  $('site-list').innerHTML = (d.sites||[]).map(s=>`
    <label class="site-opt ${s.available?'':'disabled'}">
      <input type="checkbox" value="${esc(s.name)}" ${s.available&&def.includes(s.name)?'checked':''} ${s.available?'':'disabled'}>
      <span>${esc(s.label)}</span>
      ${s.available?'':'<em>（不可用）</em>'}
    </label>`).join('');
  $('site-mask').classList.add('open');
}
function closeSiteModal(){ $('site-mask').classList.remove('open'); }
async function confirmRefresh(){
  const checked = [...document.querySelectorAll('#site-list input:checked')].map(i=>i.value);
  if(!checked.length){ toast('至少选择一个站点'); return; }
  closeSiteModal();
  const r = await fetch('/api/refresh', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({sites: checked})});
  const d = await r.json();
  if(!d.ok){ toast(d.error||'无法启动'); return; }
  toast('推荐任务已启动');
  $('refresh-panel').style.display='block';
  pollRefresh();
  REFRESH_TIMER = setInterval(pollRefresh, 3000);
}
async function pollRefresh(){
  const st = await (await fetch('/api/refresh-status')).json();
  $('refresh-log').innerHTML = (st.lines||[]).slice(-9).map(esc).join('<br>');
  $('refresh-title').textContent = st.running ? '⏳ 正在生成推荐…' : (st.code===0 ? '✅ 推荐已更新' : '⚠️ 任务结束（查看日志）');
  if(!st.running && st.code!==null && REFRESH_TIMER){
    clearInterval(REFRESH_TIMER); REFRESH_TIMER = null;
    setTimeout(async()=>{ $('refresh-panel').style.display='none'; await load(); }, 5000);
  }
}

/* ---------- 撤回面板 ---------- */
let UNDO = {timer:null, secs:0, mode:null, workId:null, payload:null};
function showUndoPanel(mode, workId, text, payload){
  if(UNDO.timer){ clearInterval(UNDO.timer); }
  UNDO.mode = mode; UNDO.workId = workId; UNDO.payload = payload || null; UNDO.secs = 5;
  $('undo-text').textContent = text;
  $('undo-btn').textContent = `撤销 (${UNDO.secs}s)`;
  $('undo-panel').classList.add('show');
  UNDO.timer = setInterval(()=>{
    UNDO.secs--;
    if(UNDO.secs <= 0){
      clearInterval(UNDO.timer); UNDO.timer = null;
      $('undo-panel').classList.remove('show');
      return;
    }
    $('undo-btn').textContent = `撤销 (${UNDO.secs}s)`;
  }, 1000);
}
async function undoAction(){
  if(!UNDO.workId) return;
  const mode = UNDO.mode, wid = UNDO.workId;
  if(mode === 'meh'){
    const r = await fetch('/api/feedback', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({work_id:wid, choice:'undo'})});
    const d = await r.json();
    if(d.ok){
      ITEMS = await (await fetch('/api/items')).json();
      renderChannels(); renderHome();
      // 首页滚动带是随机挑选，撤销后跳到作品所在频道页确保可见
      const back = ITEMS.find(x=>x.work_id===wid);
      if(back && back.channel) setTab(back.channel);
      toast('已撤回屏蔽，权重未受影响 ✓');
    } else toast(d.error||'已超过撤回时间');
  } else if(mode === 'merge'){
    const r = await fetch('/api/unmerge', {method:'POST'});
    const d = await r.json();
    if(d.ok){
      ITEMS = await (await fetch('/api/items')).json();
      renderChannels(); renderHome();
      const back = ITEMS.find(x=>x.work_id===wid);
      if(back && back.channel) setTab(back.channel);
      toast(`已撤回合并（恢复 ${d.restored} 条）✓`);
    } else toast(d.error||'没有可撤回的合并');
  } else if(mode === 'searchfav'){
    const r = await fetch('/api/search-fav', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(UNDO.payload||{})});
    const d = await r.json();
    if(d.ok){ await reloadLib(); await refreshStatus(); toast('已撤回收藏 ✓'); }
    else toast(d.error||'撤回失败');
  } else if(mode === 'libfav'){
    const r = await fetch('/api/feedback', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({work_id:wid, choice:'like'})});
    const d = await r.json();
    if(d.ok){
      // 同步推荐卡片的心形状态
      const it = ITEMS.find(x=>x.work_id===wid);
      if(it) it.liked=true;
      const key = wid.replace(/[^a-zA-Z0-9_-]/g,'_');
      const card = $('card-'+key);
      if(card){
        card.classList.add('liked');
        const lk = $('lk-'+key);
        if(lk){ lk.classList.add('on','heart-bounce'); lk.innerHTML='❤ 已收藏';
          setTimeout(()=>lk.classList.remove('heart-bounce'),700); }
      }
      await reloadLib(); await refreshStatus(); toast('已恢复收藏 ✓');
    }
    else toast(d.error||'撤回失败');
  } else if(mode === 'unlib'){
    const r = await fetch('/api/unlibrary', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({gid:wid, undo:true})});
    const d = await r.json();
    if(d.ok){ await reloadLib(); await refreshStatus(); toast('已恢复回收藏库 ✓'); }
    else toast(d.error||'撤回失败');
  }
  clearInterval(UNDO.timer); UNDO.timer = null;
  $('undo-panel').classList.remove('show');
}

/* ---------- 跨站搜索 ---------- */
async function doSearch(){
  const q = $('search-input').value.trim();
  if(!q){ toast('请输入搜索词'); return; }
  const author = $('search-author').checked;
  $('search-go').disabled = true;
  $('search-status').textContent = '⏳ 正在搜索四个站点…（约 10-20 秒）';
  $('search-results').innerHTML = '';
  try{
    const r = await fetch('/api/search', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({q, author})});
    const d = await r.json();
    if(!d.ok){ $('search-status').textContent = d.error || '搜索失败'; }
    else{
      const shown = (d.results||[]).reduce((s,g)=>s+(g.items||[]).length,0);
      const totalAll = (d.results||[]).reduce((s,g)=>s+(g.total||(g.items||[]).length),0);
      $('search-status').textContent = (totalAll>shown?`共约 ${totalAll} 条（本页显示 ${shown} 条）`:`共 ${shown} 条`) +
        (d.errors && d.errors.length ? '（' + d.errors.map(e=>e.site+' 不可用').join('、') + '）' : '');
      const SITE_CN = {'e-hentai':'E-Hentai','nhentai':'nHentai','wnacg':'绅士漫画','18comic':'禁漫'};
      const FAVS = new Set(((LIB.groups||[]).flatMap(g=>g.items)).filter(i=>i.fav).map(i=>i.work_id));
      $('search-results').innerHTML = (d.results||[]).map(g=>`
        <div class="sr-group">
          <div class="sr-site">${SITE_CN[g.site]||esc(g.site)} <span class="cnt">${g.total?('约 '+g.total+' 条'):((g.items||[]).length+' 条')}${g.total&&g.total>(g.items||[]).length?' · 本页显示 '+(g.items||[]).length:''}</span></div>
          ${(g.items||[]).length ? g.items.map(it=>{
            const faved = FAVS.has(g.site+':'+it.id);
            return `
            <div class="sr-row">
              ${it.cover?`<img loading="lazy" referrerpolicy="no-referrer" src="${imgUrl(it.cover)}" onerror="this.style.visibility='hidden'">`:''}
              <div class="sr-info">
                <div class="sr-title">${esc(it.title)}</div>
                ${it.author?`<div class="sr-author">✍ ${esc(it.author)}</div>`:''}
              </div>
              <a class="sr-link" href="${esc(it.url)}" target="_blank" rel="noreferrer">打开</a>
              <button class="sr-fav ${faved?'on':''}" data-site="${esc(g.site)}" data-id="${esc(it.id)}"
                data-title="${esc(it.title)}" data-author="${esc(it.author||'')}"
                data-url="${esc(it.url)}" data-cover="${esc(it.cover||'')}"
                onclick="searchFav(this)">${faved?'❤ 已收藏':'🤍 收藏'}</button>
            </div>`}).join('') : '<div class="empty" style="padding:16px">无结果</div>'}
        </div>`).join('');
    }
  }catch(e){ $('search-status').textContent = '搜索出错'; }
  $('search-go').disabled = false;
}

/* ---------- 搜索收藏 / 收藏库移出 ---------- */
async function reloadLib(){
  try{
    LIB = await (await fetch('/api/library')).json();
    renderLibrary();
  }catch(e){}
}
async function refreshStatus(){
  const st = await (await fetch('/api/status')).json();
  $('status-chip').innerHTML = `画像 <b>${st.total_works}</b> 部<br>收藏 <b>${st.n_fav ?? st.n_library}</b> 部<br>平均 <b>${st.mean_rating ?? '-'}</b> ★<br>推荐 <b>${st.n_rec}</b> 条<br>屏蔽 <b>${st.n_meh}</b> · 忽略 <b>${st.n_hide ?? 0}</b>`;
}
async function searchFav(btn){
  const payload = {site:btn.dataset.site, id:btn.dataset.id, title:btn.dataset.title,
    author:btn.dataset.author||'', url:btn.dataset.url||'', cover:btn.dataset.cover||''};
  const r = await fetch('/api/search-fav', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(payload)});
  const d = await r.json();
  if(!d.ok){ toast(d.error||'操作失败'); return; }
  if(d.faved){
    btn.classList.add('on'); btn.innerHTML='❤ 已收藏';
    await reloadLib(); await refreshStatus();
    showUndoPanel('searchfav', payload.site+':'+payload.id, '已收藏《'+payload.title+'》', payload);
  } else if(d.unliked){
    btn.classList.remove('on'); btn.innerHTML='🤍 收藏';
    await reloadLib(); await refreshStatus();
    toast('已取消收藏');
  }
}
async function toggleLibFav(){
  if(!CUR_LIB || !CUR_LIB.work_id) return;
  const wid = CUR_LIB.work_id;
  const r = await fetch('/api/feedback', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({work_id:wid, choice:'like'})});
  const d = await r.json();
  if(!d.ok){ toast(d.error||'操作失败'); return; }
  closeModal();
  if(d.unliked){
    // 同步推荐卡片的心形状态（无需刷新页面）
    const it = ITEMS.find(x=>x.work_id===wid);
    if(it) it.liked=false;
    const key = wid.replace(/[^a-zA-Z0-9_-]/g,'_');
    const card = $('card-'+key);
    if(card){
      card.classList.remove('liked');
      const lk = $('lk-'+key);
      if(lk){ lk.classList.remove('on'); lk.innerHTML='🤍 收藏'; }
    }
  }
  await reloadLib(); await refreshStatus();
  showUndoPanel('libfav', wid, '已取消收藏《'+CUR_LIB.title+'》');
}
async function unlib(){
  if(!CUR_LIB || !CUR_LIB.gid) return;
  const gid = CUR_LIB.gid;
  const r = await fetch('/api/unlibrary', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({gid})});
  const d = await r.json();
  if(!d.ok){ toast(d.error||'操作失败'); return; }
  closeModal();
  await reloadLib(); await refreshStatus();
  showUndoPanel('unlib', gid, '已移出收藏库《'+CUR_LIB.title+'》');
}

/* ---------- 侧边栏收缩 ---------- */
async function openDataDir(){
  const r = await fetch('/api/open-data', {method:'POST', headers:{'Content-Type':'application/json'},
    body:'{}'});
  const d = await r.json();
  if(!d.ok) toast('无法打开：' + (d.error||'未知错误'));
}
$('collapse-btn').addEventListener('click', ()=>{
  const collapsed = document.body.classList.toggle('sb-collapsed');
  localStorage.setItem('sb-collapsed', collapsed ? '1' : '0');
});
if(localStorage.getItem('sb-collapsed') === '1') document.body.classList.add('sb-collapsed');

/* ---------- 路由 ---------- */
function setTab(name){
  CURRENT_VIEW = name;
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active', t.dataset.view===name));
  document.querySelectorAll('.view').forEach(v=>v.classList.remove('active'));
  $('view-'+name).classList.add('active');
  if(name==='home') renderHome();
}
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>setTab(t.dataset.view));
document.addEventListener('keydown', e=>{ if(e.key==='Escape') closeModal(); });
load();
</script>
</body></html>
"""


# 封面图代理白名单（按域名后缀匹配）与 Referer 要求表
_IMG_HOSTS = (
    "ehgt.org", "e-hentai.org", "exhentai.org",
    "t.nhentai.net", "i.nhentai.net", "i3.nhentai.net", "nhentai.net",
    "gold-usergeneratedcontent.net",
    "qy0.ru",
    "18comic.vip",
)
_IMG_REFERER = {
    "gold-usergeneratedcontent.net": "https://hitomi.la/",
    "ehgt.org": "https://e-hentai.org/",
    "nhentai.net": "https://nhentai.net/",
    "qy0.ru": "https://www.wnacg.com/",
    "18comic.vip": "https://18comic.vip/",
}


def serve_image(u: str) -> tuple[int, bytes, str] | None:
    """代理封面图：部分图床要求特定 Referer（如 hitomi 的 gold-usergeneratedcontent.net），
    浏览器页面本身是 127.0.0.1，直连必 404，所以由服务端带正确 Referer 转发。"""
    try:
        host = (urlparse(u).hostname or "").lower()
    except ValueError:  # noqa: BLE001
        return None
    if not host or not any(host == h or host.endswith("." + h) for h in _IMG_HOSTS):
        return None
    if not u.startswith("https://"):
        return None
    from curl_cffi import requests as cr

    proxy = config.get("proxy", "")
    kwargs = {"impersonate": "chrome", "timeout": 20}
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}
    headers = {}
    for k, v in _IMG_REFERER.items():
        if host == k or host.endswith("." + k):
            headers["Referer"] = v
            break
    resp = cr.get(u, headers=headers, **kwargs)
    ctype = resp.headers.get("content-type", "application/octet-stream")
    if resp.status_code != 200 or len(resp.content) > 5 * 1024 * 1024:
        return (resp.status_code, b"", ctype)
    return (200, resp.content, ctype)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/":
            self._send(200, PAGE_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path.startswith("/api/img?"):
            u = (parse_qs(urlparse(self.path).query).get("u") or [""])[0]
            out = serve_image(u)
            if out is None:
                self._send(404, b"", "application/octet-stream")
            else:
                code, body, ctype = out
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(body)
        elif self.path == "/api/items":
            self._send(200, json.dumps(get_items(), ensure_ascii=False).encode("utf-8"))
        elif self.path == "/api/library":
            data = library_items()
            for g in data.get("groups", []):
                _attach_tag_pairs(g.get("items", []))
            self._send(200, json.dumps(data, ensure_ascii=False).encode("utf-8"))
        elif self.path == "/api/status":
            self._send(200, json.dumps(get_status(), ensure_ascii=False).encode("utf-8"))
        elif self.path == "/api/config":
            self._send(200, json.dumps(get_config(), ensure_ascii=False).encode("utf-8"))
        elif self.path == "/api/refresh-status":
            self._send(200, json.dumps(refresh_status(), ensure_ascii=False).encode("utf-8"))
        elif self.path == "/api/sites":
            self._send(200, json.dumps(api_sites(), ensure_ascii=False).encode("utf-8"))
        else:
            self._send(404, b'{"error":"not found"}')

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:  # noqa: BLE001
            body = {}
        if self.path == "/api/feedback":
            result = _handle_feedback(body)
            self._send(200 if result.get("ok") else 400,
                       json.dumps(result, ensure_ascii=False).encode("utf-8"))
        elif self.path == "/api/config":
            result = set_search_site(body.get("search_site", ""))
            self._send(200 if result.get("ok") else 400,
                       json.dumps(result, ensure_ascii=False).encode("utf-8"))
        elif self.path == "/api/merge":
            ids = body.get("ids") or []
            result = merge_items([str(i) for i in ids])
            self._send(200 if result.get("ok") else 400,
                       json.dumps(result, ensure_ascii=False).encode("utf-8"))
        elif self.path == "/api/refresh":
            result = start_refresh([str(s) for s in (body.get("sites") or [])] or None)
            self._send(200 if result.get("ok") else 400,
                       json.dumps(result, ensure_ascii=False).encode("utf-8"))
        elif self.path == "/api/unmerge":
            result = unmerge()
            self._send(200 if result.get("ok") else 400,
                       json.dumps(result, ensure_ascii=False).encode("utf-8"))
        elif self.path == "/api/search":
            result = api_search(body.get("q", ""), bool(body.get("author", False)))
            self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"))
        elif self.path == "/api/search-fav":
            result = search_fav(body)
            self._send(200 if result.get("ok") else 400,
                       json.dumps(result, ensure_ascii=False).encode("utf-8"))
        elif self.path == "/api/unlibrary":
            result = unlibrary(body)
            self._send(200 if result.get("ok") else 400,
                       json.dumps(result, ensure_ascii=False).encode("utf-8"))
        elif self.path == "/api/open-data":
            try:
                import os

                os.startfile(str(ROOT))  # noqa: S606 —— 本机个人应用，仅打开自身数据目录
                result = {"ok": True}
            except Exception as e:  # noqa: BLE001
                result = {"ok": False, "error": str(e)}
            self._send(200 if result.get("ok") else 400,
                       json.dumps(result, ensure_ascii=False).encode("utf-8"))
        else:
            self._send(404, b'{"error":"not found"}')

    def log_message(self, *args):  # 静默访问日志
        pass


def run(port: int = 8765, no_browser: bool = False) -> int:
    init_db()
    # 后台线程：收藏列表封面 + 站点可用性预热（弹窗秒开）
    threading.Thread(target=_safe_fetch_thumbs, daemon=True).start()
    threading.Thread(target=_sites_cache_loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"推荐 UI 已启动: {url}")
    print("收藏列表封面正在后台抓取（首次加载可能需要一两分钟出齐）。")
    print("按 Ctrl+C 退出。")
    if not no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    return 0


def _safe_fetch_thumbs() -> None:
    from .library import fetch_library_thumbs

    try:
        fetch_library_thumbs()
        print("收藏列表封面抓取完成。")
    except Exception as e:  # noqa: BLE001
        print(f"封面抓取失败（不影响使用）: {type(e).__name__} {e}")
