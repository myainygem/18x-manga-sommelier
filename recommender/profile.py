# -*- coding: utf-8 -*-
"""步骤3：偏好画像生成 —— 基于已补全元数据聚合统计，输出人读 MD + 机器读 JSON。"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .config import ROOT, config
from .db import get_conn

PROFILE_DIR = ROOT / "data" / "profile"
# 语言标签反映"用户拿到的是中文版"这一事实而非口味，不进入偏好
SKIP_NAMESPACES = {"language", "information"}


def load_matched() -> list[dict]:
    with get_conn() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                """SELECT e.*, m.gid, m.title_en, m.title_jp, m.category, m.rating, m.pages, m.tags
                   FROM entries e JOIN eh_metadata m ON m.entry_id = e.id
                   WHERE m.chosen_by != 'manual' AND m.gid IS NOT NULL
                     AND COALESCE(m.removed,0)=0"""
            )
        ]
    return rows


def _dedupe_by_gid(rows: list[dict]) -> list[dict]:
    seen: dict[int, dict] = {}
    for r in rows:
        gid = r["gid"]
        if gid and gid not in seen:
            seen[gid] = r
    return list(seen.values())


def build_profile(rows: list[dict]) -> dict:
    rows = _dedupe_by_gid(rows)
    n = len(rows)

    circle_counter: Counter[str] = Counter()
    artist_counter: Counter[str] = Counter()
    tag_counter: Counter[str] = Counter()
    ns_counter: Counter[str] = Counter()
    cat_counter: Counter[str] = Counter()
    ratings: list[float] = []
    pages: list[int] = []

    for r in rows:
        tags = json.loads(r.get("tags") or "[]")
        if r.get("circle_clean"):
            circle_counter[r["circle_clean"]] += 1
        artist_name = r.get("artist")
        if not artist_name:
            # 文件夹名没写作者时，回退到画廊的 artist: 标签
            for t in tags:
                if t.startswith("artist:"):
                    artist_name = t.split(":", 1)[1].strip()
                    break
        if artist_name:
            artist_counter[artist_name] += 1
        if r.get("category"):
            cat_counter[r["category"]] += 1
        if r.get("rating"):
            ratings.append(float(r["rating"]))
        if r.get("pages"):
            pages.append(int(r["pages"]))
        for tag in tags:
            ns = tag.split(":", 1)[0]
            if ns in SKIP_NAMESPACES:
                continue
            tag_counter[tag] += 1
            ns_counter[ns] += 1

    top_authors_n = config["recommend"]["top_authors_n"]
    top_tags_n = config["recommend"]["top_tags_n"]

    authors: list[dict] = []
    for name, cnt in circle_counter.most_common(top_authors_n):
        authors.append({"name": name, "count": cnt, "weight": round(min(cnt / 5, 1.0), 3)})
    for name, cnt in artist_counter.most_common(top_authors_n):
        authors.append({"name": name, "count": cnt, "weight": round(min(cnt / 5, 1.0), 3),
                        "source": "artist"})

    tags: list[dict] = []
    for tag, cnt in tag_counter.most_common(top_tags_n):
        tags.append({"tag": tag, "count": cnt, "weight": round(min(cnt / 5, 1.0), 3)})

    page_buckets = {"<=30": 0, "31-80": 0, "81-200": 0, "201-400": 0, ">400": 0}
    for p in pages:
        if p <= 30:
            page_buckets["<=30"] += 1
        elif p <= 80:
            page_buckets["31-80"] += 1
        elif p <= 200:
            page_buckets["81-200"] += 1
        elif p <= 400:
            page_buckets["201-400"] += 1
        else:
            page_buckets[">400"] += 1

    mean_rating = round(sum(ratings) / len(ratings), 3) if ratings else None
    sorted_ratings = sorted(ratings)
    median_rating = (
        round(sorted_ratings[len(sorted_ratings) // 2], 3) if sorted_ratings else None
    )

    zh_ratio = round(sum(1 for r in rows if r.get("lang") == "zh") / n, 3) if n else 0.0
    unc_ratio = round(sum(1 for r in rows if r.get("uncensored")) / n, 3) if n else 0.0

    return {
        "total_works": n,
        "authors": authors,
        "tags": tags,
        "namespaces": dict(ns_counter.most_common()),
        "categories": dict(cat_counter.most_common()),
        "rating": {"mean": mean_rating, "median": median_rating, "count": len(ratings)},
        "page_buckets": page_buckets,
        "zh_ratio": zh_ratio,
        "uncensored_ratio": unc_ratio,
        "pref": {
            "chinese_bonus": config["recommend"]["chinese_bonus"],
            "author_hit_weight": config["recommend"]["author_hit_weight"],
            "min_rating_count": config["recommend"]["min_rating_count"],
            "tag_hit_weight": 3.0,
            "page_bucket_weights": {"<=30": 0.5, "31-80": 1.0, "81-200": 1.0,
                                    "201-400": 0.6, ">400": 0.4},
            "min_rating_pref": round(max(3.6, (mean_rating or 4.0) - 0.3), 2),
        },
    }


def _bar(count: int, total: int, width: int = 30) -> str:
    if total <= 0:
        return ""
    return "█" * max(1, round(count / total * width))


def render_md(profile: dict) -> str:
    n = profile["total_works"]
    lines = ["# 个人偏好画像", "", f"基于 {n} 部已补全元数据的收藏统计。", ""]

    lines += ["## 口味速览", ""]
    r = profile["rating"]
    lines += [
        f"- 平均星级: **{r['mean']}**（中位数 {r['median']}；全站平均约 4.0）",
        f"- 中文版占比: **{profile['zh_ratio']*100:.0f}%**；无修版占比: **{profile['uncensored_ratio']*100:.0f}%**",
        f"- 分类分布: {', '.join(f'{k} {v}' for k, v in profile['categories'].items()) or '无'}",
        "",
    ]

    lines += ["## 页数分布", ""]
    for bucket, cnt in profile["page_buckets"].items():
        lines.append(f"- {bucket:>7}: {cnt:3d} {_bar(cnt, n)}")
    lines.append("")

    lines += ["## 作者 / 社团 TOP", ""]
    for a in profile["authors"]:
        src = " [artist]" if a.get("source") == "artist" else ""
        lines.append(f"- **{a['name']}**{src} — {a['count']} 部（权重 {a['weight']}）")
    lines.append("")

    lines += ["## 高频标签 TOP", ""]
    for t in profile["tags"]:
        lines.append(f"- `{t['tag']}` — {t['count']} 次（权重 {t['weight']}）")
    lines.append("")

    lines += ["## 标签命名空间分布", ""]
    for ns, cnt in profile["namespaces"].items():
        lines.append(f"- `{ns}:` — {cnt} 次 {_bar(cnt, sum(profile['namespaces'].values()))}")
    lines.append("")
    return "\n".join(lines)


def run() -> int:
    rows = load_matched()
    if not rows:
        print("尚无已补全元数据，请先运行 lookup（或等待后台任务完成）。")
        return 1
    profile = build_profile(rows)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    (PROFILE_DIR / "preference_profile.json").write_text(
        json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (PROFILE_DIR / "preference_profile.md").write_text(render_md(profile), encoding="utf-8")
    print(f"画像已生成: {profile['total_works']} 部作品 | 作者 {len(profile['authors'])} | "
          f"标签 {len(profile['tags'])} | 平均星级 {profile['rating']['mean']}")
    print("产物: data/profile/preference_profile.md / .json")
    return 0
