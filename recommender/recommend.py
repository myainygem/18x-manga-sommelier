# -*- coding: utf-8 -*-
"""步骤5：推荐器核心 —— 三通道候选生成、过滤、打分、跨站去重、AI 理由、入库。"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime
from difflib import SequenceMatcher

from .ai_assist import AIClient
from .config import ROOT, config
from .db import get_conn
from .library import get_aliases
from .parser import extract_author_from_title, norm_key, to_romaji
from .sites import available_adapters
from .sites.base import Work

PROFILE_PATH = ROOT / "data" / "profile" / "preference_profile.json"


def load_profile() -> dict:
    if not PROFILE_PATH.exists():
        from .profile import load_matched, build_profile, render_md

        rows = load_matched()
        if not rows:
            raise RuntimeError("尚无画像数据：请先完成 lookup 并运行 profile。")
        p = build_profile(rows)
        PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        PROFILE_PATH.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
        (PROFILE_PATH.parent / "preference_profile.md").write_text(render_md(p), encoding="utf-8")
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def _strip_brackets(s: str) -> str:
    return re.sub(r"\[[^\[\]]*\]", "", s or "").strip()


def _ident_variants(author: str, base: str) -> set[tuple[str, str]]:
    """(作者, 去括号标题) 身份对的规范变体：原文 + 罗马音。"""
    out: set[tuple[str, str]] = set()
    a = norm_key(author)
    b = norm_key(base)
    out.add((a, b))
    ra = to_romaji(author) if author else ""
    rb = to_romaji(base)
    if ra:
        out.add((norm_key(ra), b))
    if rb:
        out.add((a, norm_key(rb)))
    if ra and rb:
        out.add((norm_key(ra), norm_key(rb)))
    return out


def _author_keys(a: str) -> set[str]:
    """作者名匹配键：原文规范键 + 罗马音规范键（如 アマタニハルカ ↔ amatani haruka）。"""
    keys = {norm_key(a)}
    r = to_romaji(a)
    if r:
        keys.add(norm_key(r))
    keys.discard("")
    return keys


def load_own_keys() -> dict:
    """返回查重/过滤所需的键集合：

    - full_norms / idents / gids：收藏库身份（硬过滤）
    - rec_ids / rec_list / rec_norms：全部历史推荐（同站排除 + 跨站降权"优先没推过的"）
    - acted_*：有反馈的历史推荐（like/meh/hide，跨站硬排除）
    """
    full_norms: set[str] = set()
    idents: set[tuple[str, str]] = set()
    gids: set[str] = set()
    rec_ids: set[str] = set()
    rec_list: list[tuple[set[str], str]] = []
    rec_norms: set[str] = set()
    acted_keys: list[tuple[set[str], str]] = []
    acted_norms: set[str] = set()
    acted_by_author: dict[str, list[str]] = defaultdict(list)
    aliases = get_aliases()
    with get_conn() as conn:
        for r in conn.execute("SELECT * FROM entries"):
            e = dict(r)
            t = e.get("title_clean") or ""
            full_norms.add(norm_key(t))
            rom = to_romaji(t)
            if rom:
                full_norms.add(norm_key(rom))
            author = e.get("artist") or e.get("circle_clean") or ""
            base = t
            if e.get("volume_marker"):
                base = base.replace(e["volume_marker"], "").strip(" -_~～()[] ")
            idents |= _ident_variants(author, base)
        for r in conn.execute("SELECT gid, title_en FROM eh_metadata WHERE gid IS NOT NULL"):
            if r["gid"]:
                gids.add(str(r["gid"]))
            te = r["title_en"] or ""
            full_norms.add(norm_key(te))
            author = extract_author_from_title(te)
            idents |= _ident_variants(author, _strip_brackets(te))
        for r in conn.execute("SELECT alias_key FROM aliases"):
            full_norms.add(r["alias_key"])
        for r in conn.execute("SELECT work_id, title, author, feedback FROM recommended"):
            rec_ids.add(r["work_id"])
            t = r["title"] or ""
            nk = norm_key(t)
            rec_norms.add(nk)
            keys = _author_keys(r["author"] or "")
            if keys:
                rec_list.append((keys, t))
            if r["feedback"] in ("like", "meh", "hide"):
                # 有反馈的历史推荐：同作品（含跨站、日文/罗马音变体）一律不再推荐
                acted_norms.add(nk)
                full_norms.add(nk)
                rom = to_romaji(t)
                if rom:
                    full_norms.add(norm_key(rom))
                author = r["author"] or extract_author_from_title(t)
                idents |= _ident_variants(author, _strip_brackets(t))
                canon = aliases.canonical(r["author"] or "")
                if canon:
                    acted_by_author[canon].append(t)
                if keys:
                    acted_keys.append((keys, t))
    return {
        "full_norms": full_norms,
        "idents": idents,
        "gids": gids,
        "rec_ids": rec_ids,
        "rec_list": rec_list,
        "rec_norms": rec_norms,
        "acted_keys": acted_keys,
        "acted_norms": acted_norms,
        "acted_by_author": dict(acted_by_author),
    }


def _candidate_identity(w: Work) -> tuple[str, set[tuple[str, str]]]:
    return norm_key(w.title or ""), _ident_variants(w.author or "", _strip_brackets(w.title or ""))


def _filtered(w: Work, full_norms: set[str], idents: set[tuple[str, str]],
              gids: set[str], rec_ids: set[str]) -> bool:
    """是否应被过滤（已收藏 / 已推荐过）。"""
    if f"{w.site}:{w.work_id}" in rec_ids:
        return True
    if str(w.work_id) in gids:
        return True
    full, widents = _candidate_identity(w)
    if full and full in full_norms:
        return True
    return bool(widents & idents)


# 已推荐过（跨站同作品）的降权幅度：低于 min_score(5) 即仅作最后备选
_REC_SEEN_PENALTY = 10.0


def _match_rec(w: Work, rec_list: list[tuple[set[str], str]]) -> bool:
    """作者键相交（别名/罗马音变体）且标题相似（≥0.55）→ 视为同作品。"""
    if not rec_list:
        return False
    k = _author_keys(w.author or "")
    if not k:
        return False
    for keys, title in rec_list:
        if k & keys and _title_pair_sim(w.title or "", title) >= 0.55:
            return True
    return False


def _seen_before(w: Work, rec_list: list[tuple[set[str], str]],
                 rec_norms: set[str]) -> bool:
    """是否与历史推荐条目（任意反馈状态）跨站重合：优先推没推荐过的新作品。"""
    full = norm_key(w.title or "")
    if full and full in rec_norms:
        return True
    return _match_rec(w, rec_list)


def _title_pair_sim(a: str, b: str) -> float:
    """两个标题的相似度（含罗马音/去括号变体），用于跨站同作品合并。"""
    va = {norm_key(a), norm_key(to_romaji(a)), norm_key(_strip_brackets(a))}
    vb = {norm_key(b), norm_key(to_romaji(b)), norm_key(_strip_brackets(b))}
    best = 0.0
    for x in va:
        if not x:
            continue
        for y in vb:
            if not y:
                continue
            if len(x) >= 6 and (x in y or y in x):
                return 1.0
            best = max(best, SequenceMatcher(None, x, y).ratio())
    return best


def _dedupe(works: list[tuple[float, Work, dict]]) -> list[tuple[float, Work, dict]]:
    """跨站去重：作者一致且标题一致/相近（含别名与罗马音变体）、或同站同 ID 时合并。"""
    aliases = get_aliases()
    out: list[tuple[float, Work, dict, str, set, str, str]] = []
    for score, w, facts in works:
        full, widents = _candidate_identity(w)
        merged = False
        for item in out:
            same_site_id = w.site == item[1].site and w.work_id == item[1].work_id
            same_full = bool(full and full == item[3])
            author_agree = bool(
                widents & item[4]
                and (w.author or "") and (item[1].author or "")
            )
            canon_same = bool(
                (w.author or "") and (item[1].author or "")
                and aliases.canonical(w.author) == aliases.canonical(item[1].author)
            )
            title_close = _title_pair_sim(w.title or "", item[1].title or "") >= 0.55
            if same_site_id or same_full or author_agree or (canon_same and title_close):
                item[0] = max(item[0], score)
                item[1].is_chinese = item[1].is_chinese or w.is_chinese
                if not item[1].author and w.author:
                    item[1].author = w.author
                if not item[1].rating and w.rating:
                    item[1].rating = w.rating
                    item[1].rating_count = w.rating_count
                item[2].setdefault("alt_links", []).append({"site": w.site, "url": w.url})
                merged = True
                break
        if not merged:
            canon = aliases.canonical(w.author or "")
            out.append([score, w, facts, full, widents, f"{w.site}:{w.work_id}", canon])
    return [(it[0], it[1], it[2]) for it in out]


_SKIP_COUNTER: dict[str, int] = {}
# 收藏库查重（推荐候选 vs 用户收藏）：按规范作者分组的库内标题
_LIB_BY_AUTHOR: dict[str, list[str]] = {}
# 有反馈（like/meh/hide）历史推荐的跨站查重数据（每次 run 时重建）
_ACTED_KEYS: list[tuple[set[str], str]] = []
_ACTED_NORMS: set[str] = set()
_ACTED_BY_AUTHOR: dict[str, list[str]] = {}
_ACTED_AI_CALLS = 0
_ACTED_AI_CAP = 20
_AI_FOR_DUPE = None
_LIB_AI_CALLS = 0
_LIB_AI_CAP = 30
# 时间段优先：上次推荐时间（epoch），正式版开关 prefer_recent_since_last 控制
_LAST_REC_EPOCH: float | None = None


def _posted_epoch(w: Work) -> float | None:
    p = (w.posted or "").strip()
    if not p:
        return None
    try:
        return float(p)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(p, fmt).timestamp()
        except ValueError:
            continue
    return None


def _last_recommend_epoch() -> float | None:
    with get_conn() as conn:
        row = conn.execute("SELECT MAX(first_seen) FROM recommended").fetchone()
    if not row or not row[0]:
        return None
    try:
        return datetime.fromisoformat(str(row[0])).timestamp()
    except ValueError:
        return None


def _lib_map() -> dict[str, list[str]]:
    from .library import library_items

    data = library_items()
    out: dict[str, list[str]] = {}
    for g in data.get("groups", []):
        out[g["author"]] = [i["title"] for i in g["items"]]
    return out


def _same_as_library(w: Work) -> bool:
    """候选作品是否与收藏库中同作者的某部作品是同一部（标题相似，边界用 AI 判定）。"""
    global _LIB_AI_CALLS
    if not _LIB_BY_AUTHOR:
        return False
    canon = get_aliases().canonical(w.author or "")
    titles = _LIB_BY_AUTHOR.get(canon) or []
    if not titles:
        return False
    best = 0.0
    best_t = ""
    for t in titles:
        s = _title_pair_sim(w.title or "", t)
        if s > best:
            best = s
            best_t = t
    if best >= 0.62:
        return True
    if 0.25 <= best and _AI_FOR_DUPE is not None and _LIB_AI_CALLS < _LIB_AI_CAP:
        _LIB_AI_CALLS += 1
        try:
            v = _AI_FOR_DUPE.verify_match(
                {"circle_clean": "", "artist": w.author, "title_clean": w.title},
                best_t,
            )
            if v and v.get("same"):
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


def _filter_violation(w: Work) -> str | None:
    """雷点硬过滤：返回违规原因，通过则返回 None。"""
    f = config.get("filters", {}) or {}
    if f.get("require_chinese") and not w.is_chinese:
        return "无中文版"
    if w.category and w.category in f.get("hard_exclude_categories", []):
        return f"品类:{w.category}"
    tags = set(w.tags or [])
    if tags & set(f.get("hard_exclude_tags", [])):
        return "命中排除标签"
    blob = (" ".join(w.tags or []) + " " + (w.title or "")).lower()
    for pat in f.get("title_regex_exclude", []):
        if re.search(pat, blob):
            return f"命中模式:{pat}"
    return None


def _acted_before(w: Work) -> bool:
    """候选是否与有反馈（like/meh/hide）的历史推荐是同作品（跨站、日文/罗马音变体，
    边界情况用 AI 判定，与收藏库查重同一口径）。"""
    global _ACTED_AI_CALLS
    full = norm_key(w.title or "")
    if full and full in _ACTED_NORMS:
        return True
    if _match_rec(w, _ACTED_KEYS):
        return True
    canon = get_aliases().canonical(w.author or "")
    titles = _ACTED_BY_AUTHOR.get(canon) or []
    if not titles:
        return False
    best = 0.0
    best_t = ""
    for t in titles:
        s = _title_pair_sim(w.title or "", t)
        if s > best:
            best = s
            best_t = t
    if best >= 0.62:
        return True
    if 0.25 <= best and _AI_FOR_DUPE is not None and _ACTED_AI_CALLS < _ACTED_AI_CAP:
        _ACTED_AI_CALLS += 1
        try:
            v = _AI_FOR_DUPE.verify_match(
                {"circle_clean": "", "artist": w.author, "title_clean": w.title},
                best_t,
            )
            if v and v.get("same"):
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


def _skip(w: Work) -> bool:
    """雷点过滤 + 收藏库查重 + 已反馈推荐查重 + 计数。"""
    reason = _filter_violation(w)
    if reason:
        _SKIP_COUNTER[reason] = _SKIP_COUNTER.get(reason, 0) + 1
        return True
    if _same_as_library(w):
        _SKIP_COUNTER["收藏库已有"] = _SKIP_COUNTER.get("收藏库已有", 0) + 1
        return True
    if _acted_before(w):
        _SKIP_COUNTER["已反馈过的推荐"] = _SKIP_COUNTER.get("已反馈过的推荐", 0) + 1
        return True
    return False


def _bucket_weight(pages: int, pref: dict) -> float:
    bw = pref.get("page_bucket_weights", {})
    if pages <= 30:
        return bw.get("<=30", 0.5)
    if pages <= 80:
        return bw.get("31-80", 1.0)
    if pages <= 200:
        return bw.get("81-200", 1.0)
    if pages <= 400:
        return bw.get("201-400", 0.6)
    return bw.get(">400", 0.4)


def score_work(w: Work, profile: dict) -> tuple[float, dict]:
    """打分 + 返回命中事实（供推荐理由使用）。作者匹配支持别名（日文/罗马音/artist 标签）。"""
    pref = profile["pref"]
    aliases = get_aliases()
    author_map: dict[str, dict] = {}
    for a in profile["authors"]:
        author_map.setdefault(aliases.canonical(a["name"]), a)
    tag_map = {t["tag"]: t for t in profile["tags"]}
    # 扁平标签别名：nhentai 的 "big breasts" ↔ 画像的 "female:big breasts"
    alias_map: dict[str, dict] = {}
    for full, info in tag_map.items():
        if ":" in full:
            alias_map.setdefault(full.split(":", 1)[1], info)
    score = 0.0
    facts = {"tags_hit": []}
    author_canon = aliases.canonical(w.author or "")
    if author_canon and author_canon in author_map:
        a = author_map[author_canon]
        score += pref["author_hit_weight"] * a["weight"]
        facts["author_hit"] = {"name": w.author, "count": a["count"], "weight": a["weight"]}
    for t in w.tags:
        hit = tag_map.get(t) or alias_map.get(t)
        if hit:
            score += pref["tag_hit_weight"] * hit["weight"]
            facts["tags_hit"].append(f"{t}（你收藏 {hit['count']} 次）")
    if w.rating:
        base = max(0.0, w.rating - 3.5) * 2.0
        if w.rating_count > 0:
            base *= min(1.0, w.rating_count / 100.0)
        score += base
    if w.is_chinese:
        score += pref["chinese_bonus"]
    if w.pages:
        score += _bucket_weight(w.pages, pref)
    # 时间段优先（正式版开关）：发布于上次推荐之后的作品加分
    if config["recommend"].get("prefer_recent_since_last") and _LAST_REC_EPOCH:
        pe = _posted_epoch(w)
        if pe and pe > _LAST_REC_EPOCH:
            score += 8.0
            facts["recent"] = True
    # 雷点降权（NTR 受害视角 / 一女多男等），含扁平别名
    penalties = (config.get("filters", {}) or {}).get("penalty_tags", {})
    pen_alias: dict[str, float] = {}
    for full, val in penalties.items():
        if ":" in full:
            pen_alias.setdefault(full.split(":", 1)[1], float(val))
    for t in w.tags:
        p = penalties.get(t) or pen_alias.get(t)
        if p:
            score -= float(p)
            facts.setdefault("penalties", []).append(t)
    return round(score, 2), facts


def _precision_candidates(adapters, profile, full_norms, idents, gids, rec_ids,
                          rec_list, rec_norms, verbose=True):
    works: list[tuple[float, Work, dict]] = []
    authors = profile["authors"][:12]
    for a in authors:
        name = a["name"]
        for ad in adapters:
            try:
                found = ad.search_author(name, limit=8)
            except Exception as e:  # noqa: BLE001
                if verbose:
                    print(f"  [skip] {ad.name} 搜索作者 {name} 失败: {e}")
                continue
            for w in found:
                if _skip(w):
                    continue
                if _filtered(w, full_norms, idents, gids, rec_ids):
                    continue
                s, facts = score_work(w, profile)
                if _seen_before(w, rec_list, rec_norms):
                    s -= _REC_SEEN_PENALTY
                    facts["rec_seen"] = True
                works.append((s, w, facts))
    # 标签组合查询
    top_tags = [t["tag"] for t in profile["tags"][:8]]
    combos = [(top_tags[i], top_tags[j]) for i in range(len(top_tags)) for j in range(i + 1, len(top_tags))][:8]
    for c1, c2 in combos:
        q = f"{c1.split(':', 1)[-1]} {c2.split(':', 1)[-1]}"
        for ad in adapters:
            try:
                found = ad.search(q, limit=5)
            except Exception:  # noqa: BLE001
                continue
            for w in found:
                if _skip(w):
                    continue
                if _filtered(w, full_norms, idents, gids, rec_ids):
                    continue
                s, facts = score_work(w, profile)
                if _seen_before(w, rec_list, rec_norms):
                    s -= _REC_SEEN_PENALTY
                    facts["rec_seen"] = True
                works.append((s, w, facts))
    return works


def _trending_candidates(adapters, profile, full_norms, idents, gids, rec_ids,
                         rec_list, rec_norms, limit=10):
    top_authors = {a["name"] for a in profile["authors"][:20]}
    top_tags = {t["tag"] for t in profile["tags"][:40]}
    top_tags_flat = {t.split(":", 1)[1] for t in top_tags if ":" in t}
    works: list[tuple[float, Work, dict]] = []
    for ad in adapters:
        try:
            # 热作榜取整页候选，交给硬过滤（中文/标签）筛选
            found = ad.trending(limit=25)
        except Exception:  # noqa: BLE001
            continue
        for w in found:
            if _skip(w):
                continue
            if _filtered(w, full_norms, idents, gids, rec_ids):
                continue
            # 热作硬过滤：必须命中画像 TOP 作者或 TOP 标签
            tag_hit = (set(w.tags) & top_tags) or (set(w.tags) & top_tags_flat)
            if w.author not in top_authors and not tag_hit:
                continue
            # 品类过滤：已知品类非漫画/同人志的（cosplay/AI合集等）不推
            if w.category and w.category not in ("Manga", "Doujinshi"):
                continue
            s, facts = score_work(w, profile)
            if _seen_before(w, rec_list, rec_norms):
                s -= _REC_SEEN_PENALTY
                facts["rec_seen"] = True
            facts["trending"] = True
            works.append((s, w, facts))
    return works


def _series_candidates(adapters, profile, full_norms, idents, gids, rec_ids,
                       rec_list, rec_norms):
    works: list[tuple[float, Work, dict]] = []
    with get_conn() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                """SELECT e.*, m.gid FROM entries e
                   JOIN eh_metadata m ON m.entry_id = e.id
                   WHERE e.volume_marker IS NOT NULL AND m.gid IS NOT NULL"""
            )
        ]
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r["series_hint"] or ""].append(r)
    for hint, members in groups.items():
        if not hint:
            continue
        entry = members[0]
        circle = entry.get("circle_clean")
        t = entry.get("title_clean") or ""
        if entry.get("volume_marker"):
            t = t.replace(entry["volume_marker"], "").strip(" -_~～()[]")
        if not t:
            continue
        query = f"{circle} {t}" if circle else t
        owned = {str(m["gid"]) for m in members}
        for ad in adapters:
            try:
                found = ad.search(query, limit=6)
            except Exception:  # noqa: BLE001
                continue
            for w in found:
                if _skip(w):
                    continue
                if str(w.work_id) in owned or str(w.work_id) in gids:
                    continue
                if _filtered(w, full_norms, idents, gids, rec_ids):
                    continue
                wn = norm_key(w.title)
                base_n = norm_key(t)
                rom = to_romaji(t)
                same_series = False
                if base_n and len(base_n) >= 6 and (base_n in wn):
                    same_series = True
                elif rom and norm_key(rom) in wn:
                    same_series = True
                if not same_series:
                    continue
                s, facts = score_work(w, profile)
                if _seen_before(w, rec_list, rec_norms):
                    s -= _REC_SEEN_PENALTY
                    facts["rec_seen"] = True
                facts["series_note"] = f"你已收藏同系列《{entry['title_clean']}》，此为同系列其他卷"
                works.append((s, w, facts))
    return works


def backfill_covers() -> int:
    """为已入库但缺封面的推荐条目批量补封面（token 从链接 URL 解析）。"""
    from .db import init_db

    init_db()
    with get_conn() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM recommended WHERE primary_site='e-hentai' "
                "AND (cover_url IS NULL OR cover_url='')"
            )
        ]
    if not rows:
        print("没有缺封面的条目。")
        return 0
    items: list[tuple[int, str]] = []
    by_gid: dict[str, dict] = {}
    for r in rows:
        gid = r["work_id"].split(":", 1)[1]
        token = ""
        for link in json.loads(r["links"] or "[]"):
            m = re.search(r"/g/(\d+)/([0-9a-f]{10})/", link.get("url", ""))
            if m and m.group(1) == gid:
                token = m.group(2)
                break
        if token:
            items.append((int(gid), token))
            by_gid[gid] = r
    if not items:
        print("无法解析 token，跳过。")
        return 1
    from .sites.ehentai_adapter import EhentaiAdapter

    adapter = EhentaiAdapter()
    covers = adapter.batch_thumbs(items)
    n = 0
    with get_conn() as conn:
        for gid, url in covers.items():
            if url and gid in by_gid:
                conn.execute("UPDATE recommended SET cover_url=? WHERE work_id=?",
                             (url, f"e-hentai:{gid}"))
                n += 1
        conn.commit()
    print(f"封面补全: {n}/{len(items)} 条。")
    return 0


def purge_library_dupes() -> int:
    """清理推荐列表中与收藏库重复的条目（同作者+标题相似，边界 AI 判定）。"""
    global _LIB_BY_AUTHOR, _AI_FOR_DUPE, _LIB_AI_CALLS
    _LIB_BY_AUTHOR = _lib_map()
    ai = AIClient()
    _AI_FOR_DUPE = ai if ai.available else None
    _LIB_AI_CALLS = 0
    with get_conn() as conn:
        rows = [
            dict(r)
            for r in conn.execute("SELECT * FROM recommended ORDER BY score DESC")
        ]
    removed: list[tuple[str, str]] = []
    for r in rows:
        w = Work(
            site=r["primary_site"],
            work_id=r["work_id"].split(":", 1)[1],
            title=r["title"],
            author=r["author"],
            tags=json.loads(r["tags"] or "[]"),
        )
        if _same_as_library(w):
            removed.append((r["work_id"], r["title"]))
    with get_conn() as conn:
        for wid, _ in removed:
            conn.execute("DELETE FROM recommended WHERE work_id=?", (wid,))
        conn.commit()
    print(f"清理收藏库重复推荐: {len(removed)} 条")
    for _, t in removed:
        print(f"  - {t[:60]}")
    return 0


def _template_reason(w: Work, facts: dict, channel: str) -> str:
    parts = []
    if facts.get("series_note"):
        parts.append(facts["series_note"])
    if facts.get("author_hit"):
        a = facts["author_hit"]
        parts.append(f"作者 {a['name']} 命中你的收藏偏好（你已收其 {a['count']} 部作品）")
    if facts.get("tags_hit"):
        parts.append("标签命中：" + "、".join(facts["tags_hit"][:4]))
    if w.rating:
        cnt = f"，{w.rating_count} 人评" if w.rating_count else ""
        parts.append(f"评分 {w.rating:.2f}{cnt}")
    if w.pages:
        parts.append(f"{w.pages} 页")
    if w.is_chinese:
        parts.append("有中文版")
    if channel == "trending":
        parts.append("近期热门且符合你的口味画像")
    if not parts:
        parts.append("与你的画像存在部分标签重合")
    return "；".join(parts) + "。"


def run(channels: list[str] | None = None, quota: int | None = None,
        no_ai: bool = False, verbose: bool = True, force: bool = False,
        sites: list[str] | None = None) -> int:
    from .eh_lookup import lookup_running

    if not force and lookup_running():
        print("检测到元数据补全任务正在运行（共享站点限速配额）。")
        print("请等它结束后再生成推荐；如确要并行，加 --force。")
        return 2

    # 先把积累的 收藏/不感兴趣 反馈统一折算进画像（只在更新推荐时执行一次）
    try:
        from .feedback import apply_feedback as _apply_feedback

        _apply_feedback()
    except Exception:  # noqa: BLE001
        pass

    # 补全"搜索收藏"条目的元数据与鉴赏理由
    try:
        from .enrich import complete_search_favs

        complete_search_favs()
    except Exception:  # noqa: BLE001
        pass

    profile = load_profile()
    own = load_own_keys()
    full_norms, idents, own_gids = own["full_norms"], own["idents"], own["gids"]
    rec_ids, rec_list, rec_norms = own["rec_ids"], own["rec_list"], own["rec_norms"]
    global _ACTED_KEYS, _ACTED_NORMS, _ACTED_BY_AUTHOR, _ACTED_AI_CALLS
    _ACTED_KEYS = own["acted_keys"]
    _ACTED_NORMS = own["acted_norms"]
    _ACTED_BY_AUTHOR = own["acted_by_author"]
    _ACTED_AI_CALLS = 0
    _SKIP_COUNTER.clear()
    adapters = available_adapters()
    if sites:
        adapters = [a for a in adapters if a.name in sites]
        if not adapters:
            print(f"所选站点均不可用: {sites}")
            return 1
    if not adapters:
        print("没有可用站点适配器，无法生成推荐。")
        return 1
    print(f"可用站点: {', '.join(a.name for a in adapters)}")

    quota = quota or config["recommend"]["quota"]
    all_channels = channels or ["precision", "trending", "series"]
    if channels is None:
        # 固定配额：精准/热作各 10，连载 0 = 不设上限
        n_prec = int(config["recommend"].get("quota_precision", 10))
        n_trend = int(config["recommend"].get("quota_trending", 10))
        n_series = int(config["recommend"].get("quota_series", 0))
    else:
        n_prec = n_trend = n_series = 0
        if "precision" in all_channels:
            n_prec = quota
        if "trending" in all_channels:
            n_trend = quota
        if "series" in all_channels:
            n_series = quota

    ai = AIClient()
    ai_reason_on = (not no_ai) and ai.available and ai.enabled("reasons")

    # 收藏库查重上下文
    global _LIB_BY_AUTHOR, _AI_FOR_DUPE, _LIB_AI_CALLS, _LAST_REC_EPOCH
    _LIB_BY_AUTHOR = _lib_map()
    _AI_FOR_DUPE = ai if ai.available else None
    _LIB_AI_CALLS = 0
    _LAST_REC_EPOCH = _last_recommend_epoch()

    picked: list[tuple[float, Work, dict, str]] = []
    min_score = float(config["recommend"].get("min_score", 5.0))

    def pick(works, channel, n):
        nonlocal picked
        deduped = _dedupe(works)
        deduped.sort(key=lambda x: -x[0])
        for s, w, facts in deduped:
            if n > 0 and len([p for p in picked if p[3] == channel]) >= n:
                break
            if s < min_score:
                continue
            if any(w.work_id == p[1].work_id and w.site == p[1].site for p in picked):
                continue
            picked.append((s, w, facts, channel))

    if "precision" in all_channels and n_prec:
        print("通道1: 精准推荐（作者 + 标签组合）...")
        pick(_precision_candidates(adapters, profile, full_norms, idents, own_gids, rec_ids,
                                   rec_list, rec_norms, verbose),
             "precision", n_prec)
    if "trending" in all_channels and n_trend:
        print("通道2: 热作推荐（硬过滤：命中画像）...")
        pick(_trending_candidates(adapters, profile, full_norms, idents, own_gids, rec_ids,
                                  rec_list, rec_norms),
             "trending", n_trend)
    if "series" in all_channels and n_series:
        print("通道3: 连载追踪...")
        pick(_series_candidates(adapters, profile, full_norms, idents, own_gids, rec_ids,
                                rec_list, rec_norms),
             "series", n_series)

    # 排序（通道内分数优先，报告内按通道分组）
    now = datetime.now().isoformat(timespec="seconds")
    final: list[dict] = []
    for s, w, facts, channel in picked:
        reason = None
        if ai_reason_on:
            ai_facts = {
                "title": w.title,
                "author": w.author,
                "channel": channel,
                "rating": w.rating,
                "rating_count": w.rating_count,
                "pages": w.pages,
                "chinese_available": w.is_chinese,
                "tags_hit": facts.get("tags_hit", []),
                "author_hit": facts.get("author_hit"),
                "series_note": facts.get("series_note"),
            }
            reason = ai.write_reason(ai_facts)
        if not reason:
            reason = _template_reason(w, facts, channel)
        links = [{"site": w.site, "url": w.url}]
        links += facts.get("alt_links", [])
        cover = ""
        if w.site == "nhentai" and w.raw.get("thumbnail"):
            cover = "https://t.nhentai.net/" + str(w.raw["thumbnail"])
        elif w.site in ("18comic", "wnacg") and w.raw.get("cover"):
            cover = str(w.raw["cover"])
        final.append(
            {
                "work_id": f"{w.site}:{w.work_id}",
                "primary_site": w.site,
                "title": w.title,
                "author": w.author,
                "tags": w.tags,
                "rating": w.rating,
                "rating_count": w.rating_count,
                "pages": w.pages,
                "is_chinese": w.is_chinese,
                "channel": channel,
                "score": s,
                "reason": reason,
                "links": links,
                "cover_url": cover,
            }
        )

    # 封面批量获取（EH 一次请求返回全部）
    eh_items = [(int(w.work_id), str(w.raw.get("token") or "")) for s, w, f, ch in picked
                if w.site == "e-hentai" and w.raw.get("token")]
    if eh_items:
        try:
            from .sites.ehentai_adapter import EhentaiAdapter

            eh_adapter = next((a for a in adapters if isinstance(a, EhentaiAdapter)), None)
            if eh_adapter is None:
                eh_adapter = EhentaiAdapter()
            covers = eh_adapter.batch_thumbs(eh_items)
            for item in final:
                if item["primary_site"] == "e-hentai":
                    item["cover_url"] = covers.get(item["work_id"].split(":", 1)[1], "")
        except Exception:  # noqa: BLE001 —— 封面失败不影响推荐
            pass

    with get_conn() as conn:
        # 固定配额批次制：本次有产出的频道，旧批次标记淘汰，推荐页只显示最新一批
        # （反馈历史/收藏不受影响；无产出的频道保留旧批次避免整页清空）
        if picked:
            produced = sorted({p[3] for p in picked})
            conn.execute(
                f"UPDATE recommended SET superseded=1 WHERE channel IN "
                f"({','.join('?' * len(produced))})",
                produced,
            )
        for item in final:
            conn.execute(
                """INSERT OR REPLACE INTO recommended
                   (work_id, primary_site, title, author, tags, rating, rating_count, pages,
                    lang, channel, score, reason, links, first_seen, cover_url, superseded)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                (
                    item["work_id"], item["primary_site"], item["title"], item["author"],
                    json.dumps(item["tags"], ensure_ascii=False), item["rating"],
                    item["rating_count"], item["pages"], "zh" if item["is_chinese"] else "",
                    item["channel"], item["score"], item["reason"],
                    json.dumps(item["links"], ensure_ascii=False), now,
                    item.get("cover_url", ""),
                ),
            )

    from .report import render_md, render_html

    run_info = {
        "time": now,
        "channels": all_channels,
        "quota": quota,
        "ai_reason": ai_reason_on,
        "ai_calls": ai.calls,
        "sites": [a.name for a in adapters],
        "profile_works": profile["total_works"],
    }
    date_tag = datetime.now().strftime("%Y-%m-%d_%H%M")
    md_path = ROOT / "reports" / f"recommendation_{date_tag}.md"
    html_path = ROOT / "reports" / f"recommendation_{date_tag}.html"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_md(final, run_info), encoding="utf-8")
    html_path.write_text(render_html(final, run_info), encoding="utf-8")

    print(f"推荐完成: {len(final)} 条 -> {md_path.name} / {html_path.name}")
    if _SKIP_COUNTER:
        print("雷点过滤统计: " + ", ".join(f"{k} {v} 条" for k, v in _SKIP_COUNTER.items()))
    if ai_reason_on:
        print(f"AI 调用: {ai.calls} 次")
    # 关闭浏览器类适配器（18comic 的 Chrome 窗口等）
    for ad in adapters:
        close = getattr(ad, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass
    return 0
