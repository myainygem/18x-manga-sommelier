# -*- coding: utf-8 -*-
"""步骤2：E-Hentai 元数据补全。

流程：构造查询变体 → 搜索 → 规则预打分 → 对 top 候选拉 gdata 用 title_jpn/社团重打分
→（可选）AI 消歧 → 缓存 + 入库。只取元数据，不下载任何图片/文件。
"""
from __future__ import annotations

import html
import json
import os
import re
import time
from difflib import SequenceMatcher
from pathlib import Path

from .ai_assist import AIClient
from .config import ROOT, config
from .db import get_conn
from .http_client import HttpClient, HttpError
from .parser import is_generic_title, norm_key, to_romaji

METADATA_DIR = ROOT / "data" / "metadata"
GDATA_CACHE_PATH = METADATA_DIR / "_gdata_cache.json"
RUNNING_PID_PATH = ROOT / "data" / ".lookup_running.pid"

ITEM_RE = re.compile(
    r'<a href="https://e-hentai\.org/g/(\d+)/([0-9a-f]{10})/"\s*><div class="glink">(.*?)</div>',
    re.S,
)
CAT_RE = re.compile(r'<div class="cn ct\d+"[^>]*>([^<]*)</div>')
PAGES_RE = re.compile(r"<div>(\d+)\s*pages</div>")
DATE_RE = re.compile(r'id="posted_?\d*"[^>]*>([^<]+)</div>')

_AI = None


def get_ai() -> AIClient | None:
    global _AI
    if _AI is None:
        _AI = AIClient()
    return _AI


def search_variants(entry: dict) -> list[str]:
    """查询词变体，按特异性排序；AI 扩展失败时静默跳过。"""
    qs: list[str] = []
    t = entry.get("title_clean") or ""
    circle = entry.get("circle_clean")
    artist = entry.get("artist")
    raw_title = entry.get("title") or ""

    if circle and t:
        qs.append(f"{circle} {t}".strip())
    if artist and t:
        qs.append(f"{artist} {t}".strip())
    if t:
        qs.append(t)
    if "?" in raw_title:
        for rep in ("♡", "♥", "・", ""):
            v = raw_title.replace("?", rep).strip()
            if v and v not in qs:
                qs.append(v)
    # 标题首段（截至第一个感叹/心形/问号），应对副标题写法的差异
    prefix = re.split(r"[！!?？♪♡♥・~～\s]", t)[0].strip()
    if prefix and len(prefix) >= 6 and prefix not in qs:
        qs.append(prefix)
    rom = to_romaji(t)
    if rom and rom != t and rom not in qs:
        qs.append(rom)
    vol = entry.get("volume_marker")
    if vol:
        s = t.replace(vol, "").strip(" -_~～()[]")
        if s and s not in qs:
            qs.append(s)
    parts = re.split(r"\s{2,}", t)
    if len(parts) > 1:
        for p in parts:
            p = p.strip()
            if len(p) >= 4 and p not in qs:
                qs.append(p)
    return qs


def search(client: HttpClient, query: str) -> list[dict]:
    url_tpl = config["lookup"]["ehentai_search_url"]
    r = client.get(url_tpl.format(q=query), "e-hentai.org")
    results: list[dict] = []
    for m in ITEM_RE.finditer(r.text):
        results.append(
            {
                "gid": int(m.group(1)),
                "token": m.group(2),
                "title": html.unescape(re.sub(r"<[^>]+>", "", m.group(3))).strip(),
            }
        )
    chunks = re.split(r"<tr>", r.text)
    chunk_by_gid: dict[int, str] = {}
    for ch in chunks:
        lm = re.search(r'href="https://e-hentai\.org/g/(\d+)/', ch)
        if lm:
            chunk_by_gid[int(lm.group(1))] = ch
    for res in results:
        ch = chunk_by_gid.get(res["gid"], "")
        cm = CAT_RE.search(ch)
        if cm:
            res["category"] = cm.group(1).strip()
        pm = PAGES_RE.search(ch)
        if pm:
            res["pages"] = int(pm.group(1))
        dm = DATE_RE.search(ch)
        if dm:
            res["posted"] = dm.group(1).strip()
    return results


def _title_sim(entry: dict, cand_title: str) -> float:
    """标题相似度：规范化双向包含 + 假名/汉字罗马音变体 + SequenceMatcher。"""
    cand = norm_key(cand_title)
    if not cand:
        return 0.0
    t = entry.get("title_clean") or ""
    variants = [norm_key(t), norm_key(to_romaji(t))]
    series = entry.get("series_hint") or ""
    if "|" in series:
        variants.append(norm_key(series.split("|")[-1]))
    best = 0.0
    for v in variants:
        if not v:
            continue
        if len(v) >= 6 and (v in cand or cand in v):
            return 1.0
        best = max(best, SequenceMatcher(None, v, cand).ratio())
    return best


def score_meta(entry: dict, meta: dict) -> float:
    """基于 gdata 完整元数据的打分：title_jpn/title_en 双向比较 + 社团/作者（含罗马音）+ 中文版。"""
    title_jp = meta.get("title_jpn") or ""
    title_en = meta.get("title_en") or ""
    t_sim = max(_title_sim(entry, title_en), _title_sim(entry, title_jp))
    combined = norm_key(title_en) + norm_key(title_jp)
    circle = entry.get("circle_clean")
    artist = entry.get("artist")
    circle_hit = 0.0
    if circle:
        c_variants = {norm_key(circle), norm_key(to_romaji(circle))}
        for v in c_variants:
            if not v:
                continue
            if v in combined:
                circle_hit = 1.0
                break
        if circle_hit == 0.0:
            # 候选标题开头的 [社团] 括号内容单独比对（社团名对社团名）
            m = re.match(r"^\[([^\]]+)\]", title_en)
            if m:
                bracket = norm_key(m.group(1)) + norm_key(to_romaji(m.group(1)))
                for v in c_variants:
                    if v and len(v) >= 4:
                        ratio = SequenceMatcher(None, v, bracket).ratio()
                        if ratio >= 0.8:
                            circle_hit = 0.7 if ratio < 0.95 else 1.0
                            break
        if circle_hit < 0.5:
            # 汉化组上传版：真实作者在 artist: 标签里
            for t in meta.get("tags", []) or []:
                ts = str(t)
                if not ts.startswith("artist:"):
                    continue
                an = norm_key(ts.split(":", 1)[1])
                for v in c_variants:
                    if v and len(v) >= 4 and v in an:
                        circle_hit = max(circle_hit, 0.5)
                        break
    artist_hit = 0.0
    if artist:
        a_variants = {norm_key(artist), norm_key(to_romaji(artist))}
        for v in a_variants:
            if v and v in combined:
                artist_hit = 1.0
                break
        if artist_hit == 0.0:
            m = re.match(r"^\[[^\]]*\(([^)]+)\)\]", title_en)
            if m:
                bracket = norm_key(m.group(1)) + norm_key(to_romaji(m.group(1)))
                for v in a_variants:
                    if v and len(v) >= 4:
                        ratio = SequenceMatcher(None, v, bracket).ratio()
                        if ratio >= 0.8:
                            artist_hit = 0.7 if ratio < 0.95 else 1.0
                            break
    zh_hit = 0.0
    other_lang = 0.0
    unc_bonus = 0.0
    if entry.get("lang") == "zh":
        low = title_en.lower()
        if any(k in low for k in ("中国翻訳", "中文", "[chinese]", "漢化", "汉化", "chinese]")):
            zh_hit = 1.0
        elif any(k in low for k in ("[korean]", "[spanish]", "[english]", "[russian]", "한국어")):
            other_lang = 1.0
    if entry.get("uncensored") and ("decensored" in title_en.lower()
                                    or "無修正" in title_en or "无修正" in title_en):
        unc_bonus = 1.0
    return (0.50 * t_sim + 0.25 * circle_hit + 0.10 * artist_hit + 0.15 * (zh_hit - other_lang)
            + 0.10 * unc_bonus)


_gdata_cache: dict[int, dict] | None = None


def load_gdata_cache() -> dict[int, dict]:
    global _gdata_cache
    if _gdata_cache is None:
        if GDATA_CACHE_PATH.exists():
            try:
                _gdata_cache = {
                    int(k): v for k, v in json.loads(GDATA_CACHE_PATH.read_text(encoding="utf-8")).items()
                }
            except Exception:  # noqa: BLE001
                _gdata_cache = {}
        else:
            _gdata_cache = {}
    return _gdata_cache


def save_gdata_cache() -> None:
    if _gdata_cache:
        GDATA_CACHE_PATH.write_text(
            json.dumps({str(k): v for k, v in _gdata_cache.items()}, ensure_ascii=False),
            encoding="utf-8",
        )


def gdata(client: HttpClient, gid: int, token: str) -> dict | None:
    cache = load_gdata_cache()
    if gid in cache:
        return cache[gid]
    r = client.post_json(
        "https://api.e-hentai.org/api.php",
        "e-hentai.org",
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
        "filesize": meta.get("filesize"),
        "rating": float(rating) if rating else None,
        "torrentcount": int(meta.get("torrentcount") or 0),
        "tags": meta.get("tags", []),
    }
    cache[gid] = out
    return out


def _accepts(score: float, second: float, min_conf: float, margin: float, n: int) -> bool:
    if score < min_conf:
        return False
    if n == 1:
        return True
    return score - second >= margin or score >= 0.75


def _candidate_brief(cands: list[dict], metas: dict[int, dict]) -> list[dict]:
    brief = []
    for i, c in enumerate(cands[:5]):
        m = metas.get(c["gid"])
        brief.append(
            {
                "index": i,
                "title": (m or {}).get("title_en") or c["title"],
                "title_jpn": (m or {}).get("title_jpn") or "",
                "category": (m or {}).get("category") or c.get("category", ""),
                "pages": (m or {}).get("filecount") or c.get("pages"),
                "posted": c.get("posted"),
                "url": f"https://e-hentai.org/g/{c['gid']}/{c['token']}/",
            }
        )
    return brief


def _add_manual(entry: dict, text: str, candidates: list[dict] | None = None,
                used_query: str | None = None) -> None:
    with get_conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO eh_metadata (entry_id, chosen_by, confidence) VALUES (?,?,?)",
            (entry["id"], "manual", 0.0),
        )
    with open(ROOT / "data" / "manual_review.txt", "a", encoding="utf-8") as f:
        f.write(text + "\n\n")
    # 缓存候选列表，供 manual-pick 命令选择
    cache = {
        "entry_line": entry["line_no"],
        "raw": entry["raw"],
        "query_used": used_query,
        "decision": "manual",
        "confidence": 0.0,
        "candidates": [
            {
                "gid": c["gid"], "token": c["token"], "title": c["title"],
                "category": c.get("category", ""), "pages": c.get("pages"),
                "posted": c.get("posted"),
                "url": f"https://e-hentai.org/g/{c['gid']}/{c['token']}/",
            }
            for c in (candidates or [])[:10]
        ],
    }
    (METADATA_DIR / f"L{entry['line_no']:04d}.json").write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def run(limit: int | None = None, quiet: bool = False, reset_manual: bool = False,
        reset_all: bool = False) -> int:
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    if lookup_running():
        print("检测到已有元数据补全任务在运行，请勿重复启动（如需并行请先停止旧任务）。")
        return 2
    RUNNING_PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    try:
        return _run_inner(limit, quiet, reset_manual, reset_all)
    finally:
        try:
            RUNNING_PID_PATH.unlink()
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def lookup_running() -> bool:
    if not RUNNING_PID_PATH.exists():
        return False
    try:
        pid = int(RUNNING_PID_PATH.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return False
    return _pid_alive(pid)


def _run_inner(limit: int | None = None, quiet: bool = False, reset_manual: bool = False,
               reset_all: bool = False) -> int:
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    client = HttpClient()
    conn = get_conn()
    if reset_all:
        conn.execute("DELETE FROM eh_metadata")
        conn.commit()
    elif reset_manual:
        conn.execute("DELETE FROM eh_metadata WHERE chosen_by='manual'")
        conn.commit()
    entries = [dict(r) for r in conn.execute("SELECT * FROM entries ORDER BY line_no")]
    done = {r[0] for r in conn.execute("SELECT entry_id FROM eh_metadata")}
    conn.close()

    pending = [e for e in entries if e["id"] not in done]

    # 通用标题（如"第一卷"“近期汉化合集”）且无社团/作者的条目：直接跳过，不浪费查询
    skip_list: list[str] = []
    real_pending: list[dict] = []
    for e in pending:
        if (not e.get("circle_clean") and not e.get("artist")
                and is_generic_title(e.get("title_clean") or "")):
            with get_conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO eh_metadata (entry_id, chosen_by, confidence) "
                    "VALUES (?,?,?)",
                    (e["id"], "skip", 0.0),
                )
            skip_list.append(f"L{e['line_no']} | {e['raw']}")
        else:
            real_pending.append(e)
    if skip_list:
        (ROOT / "data" / "lookup_skipped.txt").write_text(
            "# 因标题为通用占位（非作品名）而跳过元数据查询的条目\n\n"
            + "\n".join(skip_list),
            encoding="utf-8",
        )
        print(f"已跳过 {len(skip_list)} 条通用标题条目（见 data/lookup_skipped.txt）")
    pending = real_pending

    if limit is not None:
        pending = pending[:limit]
    if not pending:
        print("没有待补全的条目。")
        return 0

    ai = get_ai()
    ai_state = "可用" if ai.available else "未配置/关闭（自动降级为规则匹配）"
    print(f"待补全 {len(pending)} 条 | AI 消歧: {ai_state} | 限速 "
          f"{config['rate_limits']['e-hentai.org']}s/请求")

    min_conf = config["lookup"]["match_confidence_min"]
    margin = config["lookup"]["ai_disambiguate_margin"]
    stats = {"rule": 0, "ai": 0, "manual": 0, "no_hits": 0, "gdata_fail": 0}
    t0 = time.time()

    with open(ROOT / "data" / "manual_review.txt", "w", encoding="utf-8") as f:
        f.write("# 人工确认清单（规则分未达阈值或无搜索结果的条目）\n\n")

    for i, entry in enumerate(pending, 1):
        variants = search_variants(entry)
        cands: list[dict] = []
        used_query = None
        for q in variants:
            try:
                cands = search(client, q)
            except HttpError as e:
                if e.status in (403, 429, 509):
                    print(f"[{i}/{len(pending)}] 站点拒绝 ({e.status})，终止。进度已入库，可续跑。")
                    save_gdata_cache()
                    return 2
                cands = []
            if cands:
                used_query = q
                break

        # 标题查询全失败时，用社团名兜底再搜一次
        if not cands and entry.get("circle_clean"):
            try:
                cands = search(client, entry["circle_clean"])
                if cands:
                    used_query = entry["circle_clean"] + "（社团兜底）"
            except HttpError:
                cands = []

        # 仍无结果时，AI 生成多语言查询词再试（懒调用，节省额度）
        if not cands and ai.available and ai.enabled("search_expand"):
            extra = ai.expand_queries(
                entry.get("circle_clean") or "",
                entry.get("artist") or "",
                entry.get("title_clean") or "",
            )
            for q in extra or []:
                try:
                    cands = search(client, q)
                except HttpError:
                    cands = []
                if cands:
                    used_query = q + "（AI扩展）"
                    break

        if not cands:
            stats["no_hits"] += 1
            _add_manual(entry, f"L{entry['line_no']} | {entry['raw']} | 原因: 所有查询变体均无搜索结果",
                        None, used_query)
            _progress(i, len(pending), stats, t0, quiet)
            continue

        # 预打分排序（决定 gdata 检查顺序）
        metas: dict[int, dict] = {}
        checked: list[tuple[float, dict, dict]] = []  # (score, cand, meta)
        chosen = None

        def accepted() -> bool:
            if not checked:
                return False
            second = checked[1][0] if len(checked) > 1 else 0.0
            return _accepts(checked[0][0], second, min_conf, margin, len(checked))

        def check_pool(pool: list[dict], n_top: int) -> None:
            prelim = sorted(
                ((_title_sim(entry, c["title"]), c) for c in pool), key=lambda x: -x[0]
            )
            for _, cand in prelim[:n_top]:
                if cand["gid"] in metas:
                    continue
                meta = gdata(client, cand["gid"], cand["token"])
                if meta is None:
                    stats["gdata_fail"] += 1
                    continue
                metas[cand["gid"]] = meta
                s = score_meta(entry, meta)
                checked.append((s, cand, meta))
                checked.sort(key=lambda x: -x[0])
                if accepted():
                    break

        check_pool(cands, 3)

        def try_ai() -> bool:
            nonlocal chosen
            if not (ai.available and ai.enabled("disambiguate")):
                return False
            ai_in = {
                "circle": entry.get("circle_clean"),
                "artist": entry.get("artist"),
                "title": entry.get("title_clean"),
                "lang": entry.get("lang"),
            }
            r = ai.disambiguate(ai_in, _candidate_brief(cands, metas))
            if r and r.get("index", -1) >= 0 and r.get("confidence", 0) >= 0.5:
                cand = cands[r["index"]]
                meta = metas.get(cand["gid"]) or gdata(client, cand["gid"], cand["token"])
                if meta:
                    chosen = (cand, meta, "ai", float(r["confidence"]), r.get("reason"))
                    return True
            return False

        # 规则未确定 → 先让 AI 从现有候选里选（比社团兜底搜索便宜得多）
        if not accepted():
            try_ai()

        if chosen is None and accepted():
            best_score, best_cand, best_meta = checked[0]
            chosen = (best_cand, best_meta, "rule", best_score, None)

        # AI 无匹配 → 社团名兜底搜索 → 规则复评 → AI 再试
        if chosen is None and entry.get("circle_clean"):
            try:
                more = search(client, entry["circle_clean"])
                if more:
                    used_query = entry["circle_clean"] + "（社团兜底）"
                    seen = {c["gid"] for c in cands}
                    cands += [c for c in more if c["gid"] not in seen][:8]
                    check_pool(cands, 8)
            except HttpError:
                pass

        if chosen is None and accepted():
            best_score, best_cand, best_meta = checked[0]
            chosen = (best_cand, best_meta, "rule", best_score, None)
        elif chosen is None:
            try_ai()

        # 仍未确定 → 人工
        if chosen is None:
            stats["manual"] += 1
            top5 = "\n".join(
                f"    [{j}] {c['title']} ({c.get('category','')}, {c.get('pages','?')}p) "
                f"https://e-hentai.org/g/{c['gid']}/{c['token']}/"
                for j, c in enumerate(cands[:5])
            )
            best_note = (
                f"gdata复打分 {checked[0][0]:.2f} "
                f"(title_jpn={checked[0][2].get('title_jpn','')!r})"
                if checked else "gdata 获取失败"
            )
            _add_manual(
                entry,
                f"L{entry['line_no']} | {entry['raw']} | 查询: {used_query} | {best_note}，候选:\n{top5}",
                cands,
                used_query,
            )
            _progress(i, len(pending), stats, t0, quiet)
            continue

        cand, meta, decision, confidence, ai_reason = chosen
        if decision == "rule":
            stats["rule"] += 1
        else:
            stats["ai"] += 1

        cache = {
            "entry_line": entry["line_no"],
            "query_used": used_query,
            "decision": decision,
            "confidence": confidence,
            "ai_reason": ai_reason,
            "gdata": meta,
        }
        (METADATA_DIR / f"L{entry['line_no']:04d}.json").write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        with get_conn() as c2:
            c2.execute(
                """INSERT OR REPLACE INTO eh_metadata
                   (entry_id, gid, token, title_en, title_jp, category, lang, pages,
                    rating, rating_count, uploader, posted, tags, parent_gid, parent_key,
                    chosen_by, confidence, raw_json_path)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    entry["id"], meta["gid"], meta["token"], meta["title_en"], meta["title_jp"],
                    meta["category"], entry["lang"], meta["filecount"],
                    meta["rating"], None, meta["uploader"], meta["posted"],
                    json.dumps(meta["tags"], ensure_ascii=False),
                    meta["gid"], meta["token"], decision, confidence,
                    f"L{entry['line_no']:04d}.json",
                ),
            )
        save_gdata_cache()
        _progress(i, len(pending), stats, t0, quiet)

    with get_conn() as c3:
        total_done = c3.execute("SELECT COUNT(*) FROM eh_metadata").fetchone()[0]
    summary = [
        "# 元数据补全汇总",
        "",
        f"- 本次处理: {len(pending)} 条",
        f"- 规则命中: {stats['rule']}",
        f"- AI 消歧命中: {stats['ai']}",
        f"- 需人工确认: {stats['manual']}",
        f"- 无搜索结果: {stats['no_hits']}",
        f"- gdata 失败: {stats['gdata_fail']}",
        f"- 库内累计已补全: {total_done} / {len(entries)}",
        f"- 总耗时: {(time.time()-t0)/60:.1f} 分钟",
        "",
        "人工确认清单见 `data/manual_review.txt`。",
    ]
    (ROOT / "data" / "metadata_summary.md").write_text("\n".join(summary), encoding="utf-8")
    print(f"完成。规则 {stats['rule']}，AI {stats['ai']}，人工 {stats['manual']}，"
          f"无结果 {stats['no_hits']}，gdata失败 {stats['gdata_fail']}。")
    return 0


def verify_ai_matches(max_n: int | None = None) -> int:
    """对全部 AI 命中的条目做复核（同一部作品判定），错配的转人工。"""
    ai = get_ai()
    if not ai.available:
        print("AI 不可用，无法复核（请检查 DEEPSEEK_API_KEY）。")
        return 1
    with get_conn() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                """SELECT e.*, m.title_en, m.title_jp FROM entries e
                   JOIN eh_metadata m ON m.entry_id = e.id
                   WHERE m.chosen_by='ai' ORDER BY e.line_no"""
            )
        ]
    if not rows:
        print("没有 AI 命中的条目。")
        return 0
    print(f"待复核 AI 命中: {len(rows)} 条")
    downgraded = 0
    ok = 0
    for i, r in enumerate(rows, 1):
        if max_n is not None and i > max_n:
            break
        v = ai.verify_match(
            {"circle_clean": r.get("circle_clean"), "artist": r.get("artist"),
             "title_clean": r.get("title_clean")},
            (r.get("title_en") or "") + (" / " + r.get("title_jp") if r.get("title_jp") else ""),
        )
        if v is None:
            print(f"[{i}/{len(rows)}] L{r['line_no']} 复核调用失败，保留原匹配")
            continue
        if v.get("same"):
            ok += 1
            print(f"[{i}/{len(rows)}] L{r['line_no']} ✓ 同一部（{v.get('reason', '')[:40]}）")
        else:
            downgraded += 1
            with get_conn() as conn:
                conn.execute(
                    "UPDATE eh_metadata SET chosen_by='manual', confidence=0, "
                    "gid=NULL, token=NULL, title_en=NULL, title_jp=NULL, category=NULL, "
                    "pages=NULL, rating=NULL, uploader=NULL, posted=NULL, tags=NULL, "
                    "parent_gid=NULL, parent_key=NULL, raw_json_path=NULL WHERE entry_id=?",
                    (r["id"],),
                )
            print(f"[{i}/{len(rows)}] L{r['line_no']} ✗ 不是同一部 → 转人工 | "
                  f"源《{r['title_clean'][:25]}》 vs 《{(r['title_en'] or '')[:45]}》")
    print(f"复核完成: 同一部 {ok} 条, 错配降级 {downgraded} 条。")
    return 0


def auto_resolve(max_n: int | None = None) -> int:
    """人工条目攻坚通道：AI 多语言检索词 → 扩大候选池 → AI 选择 → AI 复核。"""
    ai = get_ai()
    if not ai.available:
        print("AI 不可用（请配置 DEEPSEEK_API_KEY）。")
        return 1
    client = HttpClient()
    with get_conn() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                """SELECT e.* FROM entries e JOIN eh_metadata m ON m.entry_id = e.id
                   WHERE m.chosen_by='manual' ORDER BY e.line_no"""
            )
        ]
    if max_n is not None:
        rows = rows[:max_n]
    if not rows:
        print("没有待攻坚的人工条目。")
        return 0
    print(f"待攻坚人工条目: {len(rows)} 条")
    resolved = 0
    for i, entry in enumerate(rows, 1):
        t = entry.get("title_clean") or ""
        tg = entry.get("translator_group")
        queries: list[str] = []
        if tg:
            queries.append(tg)
            if len(t) >= 2:
                queries.append(f"{tg} {t[:12]}")
        advanced = ai.advanced_queries(
            entry.get("circle_clean") or "", entry.get("artist") or "", t, tg or ""
        )
        for q in advanced or []:
            if isinstance(q, str) and q.strip() and q not in queries:
                queries.append(q.strip())
        # 跨站桥接：用 wnacg（中文站，独立限速）找规范标题，还原被 '?' 损坏的字符
        try:
            from .sites.wnacg_adapter import WnacgAdapter

            wn = WnacgAdapter()
            for x in wn.search(t, limit=5):
                xt = x.title or ""
                if (norm_key(t) and norm_key(t) in norm_key(xt)) or (
                    len(norm_key(t)) >= 6
                    and SequenceMatcher(None, norm_key(t), norm_key(xt)).ratio() > 0.55
                ):
                    bridge = re.sub(r"\[[^\[\]]*\]\s*$", "", xt).strip()
                    if bridge and bridge not in queries:
                        queries.append(bridge)
                        break
        except Exception:  # noqa: BLE001 —— wnacg 桥接失败不影响主流程
            pass
        pool: dict[int, dict] = {}
        for q in queries[:10]:
            try:
                cands = search(client, q)
            except HttpError as e:
                if e.status in (403, 429, 509):
                    print(f"[{i}/{len(rows)}] 站点拒绝 ({e.status})，终止。")
                    return 2
                cands = []
            for c in cands:
                pool.setdefault(c["gid"], c)
            if len(pool) >= 12:
                break
        if not pool and entry.get("circle_clean"):
            try:
                for c in search(client, entry["circle_clean"]):
                    pool.setdefault(c["gid"], c)
            except HttpError:
                pass
        if not pool:
            print(f"[{i}/{len(rows)}] L{entry['line_no']} 仍无候选，保持人工")
            continue
        cand_list = list(pool.values())
        r = ai.disambiguate(
            {
                "circle": entry.get("circle_clean"),
                "artist": entry.get("artist"),
                "title": t,
                "lang": entry.get("lang"),
            },
            _candidate_brief(cand_list, {}),
        )
        if not r or r.get("index", -1) < 0 or r.get("confidence", 0) < 0.5:
            print(f"[{i}/{len(rows)}] L{entry['line_no']} AI 判定无匹配，保持人工")
            continue
        cand = cand_list[r["index"]]
        meta = gdata(client, cand["gid"], cand["token"])
        if not meta:
            print(f"[{i}/{len(rows)}] L{entry['line_no']} gdata 获取失败")
            continue
        v = ai.verify_match(
            {
                "circle_clean": entry.get("circle_clean"),
                "artist": entry.get("artist"),
                "title_clean": t,
            },
            (meta.get("title_en") or "")
            + ((" / " + meta.get("title_jp")) if meta.get("title_jp") else ""),
        )
        if not v or not v.get("same"):
            print(f"[{i}/{len(rows)}] L{entry['line_no']} 复核未通过，保持人工 | "
                  f"{t[:25]} vs {(meta.get('title_en') or '')[:40]}")
            continue
        with get_conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO eh_metadata
                   (entry_id, gid, token, title_en, title_jp, category, lang, pages, rating,
                    rating_count, uploader, posted, tags, parent_gid, parent_key, chosen_by,
                    confidence, raw_json_path)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    entry["id"], meta["gid"], meta["token"], meta["title_en"], meta["title_jp"],
                    meta["category"], entry["lang"], meta["filecount"], meta["rating"], None,
                    meta["uploader"], meta["posted"], json.dumps(meta["tags"], ensure_ascii=False),
                    meta["gid"], meta["token"], "ai", float(r["confidence"]),
                    f"L{entry['line_no']:04d}.json",
                ),
            )
        cache = {
            "entry_line": entry["line_no"],
            "decision": "ai",
            "confidence": r.get("confidence"),
            "ai_reason": r.get("reason"),
            "verify_reason": v.get("reason"),
            "gdata": meta,
        }
        (METADATA_DIR / f"L{entry['line_no']:04d}.json").write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        save_gdata_cache()
        resolved += 1
        print(f"[{i}/{len(rows)}] L{entry['line_no']} ✓ 攻坚成功: "
              f"{(meta.get('title_en') or '')[:55]}")
    print(f"攻坚完成: 解决 {resolved}/{len(rows)} 条。")
    return 0


def manual_pick(line_no: int, index: int | None = None) -> int:
    """人工确认：查看/选择候选（AI 二轮之后仍存疑的条目用）。"""
    cache_path = METADATA_DIR / f"L{line_no:04d}.json"
    if not cache_path.exists():
        print(f"找不到缓存 {cache_path.name}（该条目可能已确认或未处理）。")
        return 1
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    if cache.get("decision") != "manual":
        print(f"L{line_no} 当前状态: {cache.get('decision')}，无需人工确认。")
        return 0
    cands = cache.get("candidates") or []
    if index is None:
        print(f"L{line_no} | {cache.get('raw', '')}")
        for i, c in enumerate(cands):
            print(f"  [{i}] {c['title']} ({c.get('category','')}, {c.get('pages','?')}p) {c['url']}")
        if not cands:
            print("（无候选缓存：所有查询变体均无结果，建议等 AI 二轮扩展查询）")
        return 0
    if not (0 <= index < len(cands)):
        print(f"下标 {index} 超出范围 0..{len(cands)-1}。")
        return 1
    cand = cands[index]
    client = HttpClient()
    meta = gdata(client, cand["gid"], cand["token"])
    if meta is None:
        print("gdata 获取失败，请重试。")
        return 1
    with get_conn() as conn:
        entry_id = conn.execute(
            "SELECT id FROM entries WHERE line_no=?", (line_no,)
        ).fetchone()
        if not entry_id:
            print(f"行号 {line_no} 不在库中。")
            return 1
        conn.execute(
            """INSERT OR REPLACE INTO eh_metadata
               (entry_id, gid, token, title_en, title_jp, category, lang, pages, rating,
                rating_count, uploader, posted, tags, parent_gid, parent_key, chosen_by,
                confidence, raw_json_path)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'user',1.0,?)""",
            (
                entry_id[0], meta["gid"], meta["token"], meta["title_en"], meta["title_jp"],
                meta["category"], None, meta["filecount"], meta["rating"], None,
                meta["uploader"], meta["posted"], json.dumps(meta["tags"], ensure_ascii=False),
                meta["gid"], meta["token"], cache_path.name,
            ),
        )
    cache["decision"] = "user"
    cache["chosen"] = cand
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"L{line_no} 已人工确认为: {meta['title_en']}")
    return 0


def _progress(i, total, stats, t0, quiet):
    if quiet or (i % 5 != 0 and i != total):
        return
    el = time.time() - t0
    rate = el / i
    eta = rate * (total - i)
    print(
        f"[{i}/{total}] 规则 {stats['rule']} + AI {stats['ai']} 命中, "
        f"人工 {stats['manual']}, 无结果 {stats['no_hits']} | 平均 {rate:.0f}s/条 | "
        f"ETA {eta/60:.0f} 分",
        flush=True,
    )
