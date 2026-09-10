# -*- coding: utf-8 -*-
"""推荐报告渲染：Markdown + HTML。"""
from __future__ import annotations

import html as html_mod

CHANNEL_TITLES = {
    "precision": "精准推荐（命中你的画像）",
    "trending": "热作推荐（近期热门 × 画像过滤）",
    "series": "连载追踪（你收藏系列的未收卷）",
}


def render_md(items: list[dict], info: dict) -> str:
    lines = ["# 漫画推荐报告", ""]
    lines.append(
        f"生成时间: {info['time']} | 画像基于 {info['profile_works']} 部收藏 | "
        f"站点: {', '.join(info['sites'])}"
    )
    if info.get("ai_reason"):
        lines.append("推荐理由: DeepSeek AI 生成（基于已核实事实）")
    lines.append("")
    order = ["precision", "trending", "series"]
    for ch in order:
        ch_items = [i for i in items if i["channel"] == ch]
        if not ch_items:
            continue
        lines.append(f"## {CHANNEL_TITLES.get(ch, ch)}")
        lines.append("")
        for i, item in enumerate(sorted(ch_items, key=lambda x: -x["score"]), 1):
            main_link = item["links"][0]["url"] if item["links"] else ""
            lines.append(f"### {i}. [{item['title']}]({main_link})")
            meta = []
            if item["author"]:
                meta.append(f"作者: {item['author']}")
            if item["rating"]:
                cnt = f" / {item['rating_count']} 人评" if item["rating_count"] else ""
                meta.append(f"评分: {item['rating']}{cnt}")
            if item["pages"]:
                meta.append(f"{item['pages']} 页")
            if item["is_chinese"]:
                meta.append("有中文版")
            meta.append(f"画像匹配分: {item['score']}")
            lines.append(" | ".join(meta))
            lines.append("")
            lines.append(f"> {item['reason']}")
            lines.append("")
            links_txt = "、".join(
                f"[{l['site']}]({l['url']})" for l in item["links"]
            )
            lines.append(f"链接: {links_txt}")
            lines.append("")
    if not items:
        lines.append("本期没有产生推荐（画像数据不足或候选均被过滤）。")
    lines.append("---")
    lines.append(f"AI 调用次数: {info.get('ai_calls', 0)}")
    return "\n".join(lines)


_CSS = """
body { font-family: "Segoe UI", "Microsoft YaHei", sans-serif; margin: 0; background: #f6f7f9; color: #24292f; }
.container { max-width: 900px; margin: 0 auto; padding: 24px; }
h1 { font-size: 26px; } h2 { border-bottom: 2px solid #e1e4e8; padding-bottom: 8px; margin-top: 36px; }
.card { background: #fff; border: 1px solid #e1e4e8; border-radius: 10px; padding: 16px 18px; margin: 14px 0; }
.card h3 { margin: 0 0 6px; font-size: 17px; }
.card h3 a { color: #0969da; text-decoration: none; }
.card .meta { color: #57606a; font-size: 13px; margin: 4px 0; }
.card .reason { background: #f6f8fa; border-left: 3px solid #0969da; padding: 8px 12px; margin: 10px 0; border-radius: 4px; font-size: 14px; }
.links a { display: inline-block; margin-right: 10px; font-size: 13px; }
.tag { display: inline-block; background: #ddf4ff; color: #0969da; border-radius: 20px; padding: 1px 9px; font-size: 12px; margin: 2px 4px 2px 0; }
.meta-info { color: #57606a; font-size: 13px; }
"""


def render_html(items: list[dict], info: dict) -> str:
    order = ["precision", "trending", "series"]
    body = []
    for ch in order:
        ch_items = sorted(
            (i for i in items if i["channel"] == ch), key=lambda x: -x["score"]
        )
        if not ch_items:
            continue
        body.append(f"<h2>{html_mod.escape(CHANNEL_TITLES.get(ch, ch))}</h2>")
        for i, item in enumerate(ch_items, 1):
            main_link = item["links"][0]["url"] if item["links"] else "#"
            meta = []
            if item["author"]:
                meta.append(f"作者: {html_mod.escape(item['author'])}")
            if item["rating"]:
                cnt = f" / {item['rating_count']} 人评" if item["rating_count"] else ""
                meta.append(f"评分: {item['rating']}{cnt}")
            if item["pages"]:
                meta.append(f"{item['pages']} 页")
            if item["is_chinese"]:
                meta.append("有中文版")
            meta.append(f"匹配分: {item['score']}")
            tags_html = "".join(
                f'<span class="tag">{html_mod.escape(t)}</span>' for t in item["tags"][:8]
            )
            links_html = " ".join(
                f'<a href="{html_mod.escape(l["url"])}">{html_mod.escape(l["site"])}</a>'
                for l in item["links"]
            )
            body.append(
                f"""<div class="card">
<h3>{i}. <a href="{html_mod.escape(main_link)}">{html_mod.escape(item['title'])}</a></h3>
<div class="meta">{' | '.join(meta)}</div>
<div>{tags_html}</div>
<div class="reason">{html_mod.escape(item['reason'])}</div>
<div class="links">{links_html}</div>
</div>"""
            )
    if not items:
        body.append("<p>本期没有产生推荐。</p>")
    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>漫画推荐报告</title>
<style>{_CSS}</style></head><body><div class="container">
<h1>漫画推荐报告</h1>
<p class="meta-info">生成时间: {html_mod.escape(info['time'])} | 画像基于 {info['profile_works']} 部收藏 | 站点: {html_mod.escape(', '.join(info['sites']))} | AI 调用: {info.get('ai_calls', 0)}</p>
{''.join(body)}
</div></body></html>"""
