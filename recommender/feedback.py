# -*- coding: utf-8 -*-
"""步骤6：反馈闭环 —— 记录喜欢/无感、回写画像权重、状态查看。"""
from __future__ import annotations

import json
from datetime import datetime

from .config import ROOT
from .db import get_conn
from .profile import PROFILE_DIR, load_matched, build_profile, render_md

PROFILE_PATH = PROFILE_DIR / "preference_profile.json"

LIKE_AUTHOR_DELTA = 0.15
LIKE_TAG_DELTA = 0.05
MEH_AUTHOR_DELTA = -0.10
MEH_TAG_DELTA = -0.03

_AUDIT_PATH = ROOT / "data" / "feedback_audit.log"


def _audit(action: str, detail: str = "") -> None:
    try:
        line = f"{datetime.now().isoformat(timespec='seconds')} | {action} | {detail}"
        with open(_AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def record(work_id: str, choice: str) -> int:
    if choice not in ("like", "meh", "hide"):
        print(f"无效的反馈类型: {choice}")
        return 1
    with get_conn() as conn:
        row = conn.execute("SELECT work_id FROM recommended WHERE work_id=?", (work_id,)).fetchone()
        if not row:
            print(f"找不到推荐条目: {work_id}（可用 status 查看推荐过的 ID）。")
            return 1
        conn.execute(
            "UPDATE recommended SET feedback=?, feedback_at=? WHERE work_id=?",
            (choice, datetime.now().isoformat(timespec="seconds"), work_id),
        )
    print(f"已记录: {work_id} -> {choice}")
    return 0


def _bump(items: list[dict], name: str, delta: float, count: int, key: str) -> list[dict]:
    for it in items:
        if it.get(key) == name:
            w = it.get("weight", 0.0) + delta
            it["weight"] = round(max(0.0, min(1.0, w)), 3)
            return items
    items.append({key: name, "count": count, "weight": round(max(0.0, min(1.0, delta)), 3)})
    return items


def apply_feedback() -> int:
    if not PROFILE_PATH.exists():
        print("画像文件不存在，请先运行 profile。")
        return 1
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    with get_conn() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM recommended WHERE feedback IN ('like','meh') "
                "AND COALESCE(feedback_applied,0)=0"
            )
        ]
    if not rows:
        print("没有待应用的 like/meh 反馈（每条反馈只折算一次）。")
        return 0
    authors = profile.setdefault("authors", [])
    tags = profile.setdefault("tags", [])
    for r in rows:
        delta_a = LIKE_AUTHOR_DELTA if r["feedback"] == "like" else MEH_AUTHOR_DELTA
        delta_t = LIKE_TAG_DELTA if r["feedback"] == "like" else MEH_TAG_DELTA
        if r["author"]:
            authors = _bump(authors, r["author"], delta_a, r["rating_count"] or 1, "name")
        for t in json.loads(r["tags"] or "[]"):
            tags = _bump(tags, t, delta_t, 1, "tag")
    profile["authors"] = authors
    profile["tags"] = tags
    PROFILE_PATH.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    (PROFILE_DIR / "preference_profile.md").write_text(render_md(profile), encoding="utf-8")
    # 标记已应用，防止下次重复折算
    with get_conn() as conn:
        conn.execute("UPDATE recommended SET feedback_applied=1 WHERE feedback_applied=0")
        conn.commit()
    print(f"已把 {len(rows)} 条反馈折算进画像权重（喜欢 {sum(1 for r in rows if r['feedback']=='like')}，"
          f"无感 {sum(1 for r in rows if r['feedback']=='meh')}）。")
    _audit("apply_feedback", f"{len(rows)} 条折算（like={sum(1 for r in rows if r['feedback']=='like')} "
                             f"meh={sum(1 for r in rows if r['feedback']=='meh')}）")
    return 0


def revert_feedback(work_id: str) -> bool:
    """撤销一条已折算的反馈：回滚其权重增量并清除记录（近似回滚，封顶处可能有微差）。"""
    if not PROFILE_PATH.exists():
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT feedback, feedback_applied, author, tags FROM recommended WHERE work_id=?",
            (work_id,),
        ).fetchone()
    if not row or not row[0] or not row[1]:
        return False
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    is_like = row[0] == "like"
    # 应用反向增量
    d_a = -(LIKE_AUTHOR_DELTA if is_like else MEH_AUTHOR_DELTA)
    d_t = -(LIKE_TAG_DELTA if is_like else MEH_TAG_DELTA)
    authors = profile.setdefault("authors", [])
    tags = profile.setdefault("tags", [])
    if row[2]:
        authors = _bump(authors, row[2], d_a, 1, "name")
    for t in json.loads(row[3] or "[]"):
        tags = _bump(tags, t, d_t, 1, "tag")
    profile["authors"] = authors
    profile["tags"] = tags
    PROFILE_PATH.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    (PROFILE_DIR / "preference_profile.md").write_text(render_md(profile), encoding="utf-8")
    with get_conn() as conn:
        conn.execute(
            "UPDATE recommended SET feedback=NULL, feedback_at=NULL, feedback_applied=0 "
            "WHERE work_id=?",
            (work_id,),
        )
        conn.commit()
    print(f"已回滚反馈权重: {work_id} ({row[0]})")
    _audit("revert_feedback", f"{work_id} ({row[0]})")
    return True


def status() -> int:
    with get_conn() as conn:
        n_entries = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        n_meta = conn.execute(
            "SELECT COUNT(*) FROM eh_metadata WHERE chosen_by NOT IN ('manual','skip')").fetchone()[0]
        n_manual = conn.execute("SELECT COUNT(*) FROM eh_metadata WHERE chosen_by='manual'").fetchone()[0]
        n_skip = conn.execute("SELECT COUNT(*) FROM eh_metadata WHERE chosen_by='skip'").fetchone()[0]
        n_rec = conn.execute("SELECT COUNT(*) FROM recommended").fetchone()[0]
        n_like = conn.execute("SELECT COUNT(*) FROM recommended WHERE feedback='like'").fetchone()[0]
        n_meh = conn.execute("SELECT COUNT(*) FROM recommended WHERE feedback='meh'").fetchone()[0]
        ai_rows = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0) FROM ai_usage"
        ).fetchone()
    print("== 状态 ==")
    print(f"收藏条目: {n_entries}")
    print(f"元数据已补全: {n_meta} | 待人工确认: {n_manual} | 通用标题跳过: {n_skip}")
    print(f"累计推荐: {n_rec} | 喜欢 {n_like} / 无感 {n_meh}")
    print(f"AI 调用: {ai_rows[0]} 次 | prompt {ai_rows[1]} tokens | completion {ai_rows[2]} tokens")
    return 0
