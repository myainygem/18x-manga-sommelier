# -*- coding: utf-8 -*-
"""E-Hentai 收藏画像与跨站推荐器 CLI。

用法:
  python cli.py check                     环境自检
  python cli.py parse                     解析 folder_list.txt
  python cli.py lookup                    补全 E-Hentai 元数据（长任务）
  python cli.py profile                   生成偏好画像
  python cli.py recommend [--channel ...] 生成推荐报告
  python cli.py feedback <ID> <like|meh>  记录反馈
  python cli.py update-profile            反馈回写画像权重
  python cli.py series-check              仅跑连载更新通道
  python cli.py status                    库/画像/推荐统计
"""
from __future__ import annotations

import argparse
import sys


def cmd_check(args) -> int:
    from recommender import ai_assist, db
    from recommender.config import config
    from recommender.http_client import HttpClient, check_reachable

    db.init_db()
    print("== 环境自检 ==")
    print(f"config: {config.__class__.__module__} 已加载")
    print(f"代理: {config.get('proxy') or '(直连)'}")
    client = HttpClient()
    targets = [
        ("https://api.e-hentai.org/api.php", "e-hentai.org"),
        ("https://hitomi.la/", "hitomi.la"),
        ("https://wnacg.com/", "wnacg.com"),
    ]
    if config["sites"].get("nhentai"):
        targets.append(("https://nhentai.net/api/galleries/search?query=a", "nhentai.net"))
    for url, domain in targets:
        status, msg = check_reachable(url, domain, client)
        print(f"  {domain:16s} -> {status} {msg}")

    ai = ai_assist.AIClient()
    key_state = "已配置" if ai.key else "未配置（AI 功能将自动降级）"
    print(f"DeepSeek key: {key_state}; enabled={ai.ai.get('enabled')}; "
          f"search_expand={ai.ai.get('search_expand')}; disambiguate={ai.ai.get('disambiguate')}; "
          f"reasons={ai.ai.get('reasons')}")
    if args.ai and ai.key:
        ok = ai.chat_json(
            "你是一个连通性测试助手。", "请回复 {\"ok\": true}。", "check",
            validator=lambda o: o.get("ok") is True,
        )
        print(f"DeepSeek 最小请求验证: {'成功' if ok else '失败（将降级）'}")
    return 0


def cmd_parse(_args) -> int:
    import json

    from recommender import db
    from recommender.config import ROOT
    from recommender.parser import parse_file

    src = ROOT / "folder_list.txt"
    if not src.exists():
        print(f"找不到源文件: {src}")
        return 1
    entries, failures = parse_file(src)

    utf8_copy = ROOT / "data" / "folder_list.utf8.txt"
    utf8_copy.write_text(src.read_text(encoding="gbk", errors="replace"), encoding="utf-8")

    out = ROOT / "data" / "entries.json"
    out.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    (ROOT / "data" / "parse_failures.txt").write_text(
        "\n".join(f"L{f['line_no']}: {f['raw']}  ({f['reason']})" for f in failures),
        encoding="utf-8",
    )

    db.init_db()
    db.upsert_entries(entries)

    zh = sum(1 for e in entries if e["lang"] == "zh")
    un = sum(1 for e in entries if e["uncensored"])
    vol = sum(1 for e in entries if e["volume_marker"])
    with_artist = sum(1 for e in entries if e["artist"])
    print(f"解析完成: {len(entries)} 条 / 失败 {len(failures)} 条")
    print(f"  中文: {zh} | 无修: {un} | 带卷号标记: {vol} | 带作者: {with_artist}")
    print(f"  产物: data/entries.json, data/folder_list.utf8.txt, data/parse_failures.txt")
    return 0


def cmd_lookup(args) -> int:
    from recommender import eh_lookup

    return eh_lookup.run(limit=args.limit, reset_manual=args.reset_manual, reset_all=args.reset_all)


def cmd_profile(_args) -> int:
    from recommender import profile

    return profile.run()


def cmd_recommend(args) -> int:
    from recommender import recommend

    sites = None
    if args.sites:
        sites = [s.strip() for s in args.sites.split(",") if s.strip()]
    try:
        return recommend.run(channels=args.channel, quota=args.quota, no_ai=args.no_ai,
                             force=args.force, sites=sites)
    except RuntimeError as e:
        print(f"无法生成推荐: {e}")
        return 1


def cmd_series_check(args) -> int:
    from recommender import recommend

    return recommend.run(channels=["series"], quota=args.quota, no_ai=args.no_ai,
                         force=args.force)


def cmd_status(_args) -> int:
    from recommender import feedback

    return feedback.status()


def cmd_update_profile(_args) -> int:
    from recommender import feedback

    return feedback.apply_feedback()


def cmd_manual_pick(args) -> int:
    from recommender import eh_lookup

    return eh_lookup.manual_pick(args.line_no, args.index)


def cmd_verify_ai(args) -> int:
    from recommender import eh_lookup

    return eh_lookup.verify_ai_matches(max_n=args.max)


def cmd_auto_resolve(args) -> int:
    from recommender import eh_lookup

    return eh_lookup.auto_resolve(max_n=args.max)


def cmd_serve(args) -> int:
    from recommender import serve

    return serve.run(port=args.port, no_browser=args.no_browser)


def cmd_app(_args) -> int:
    from recommender import app

    return app.run_app()


def cmd_backfill_covers(_args) -> int:
    from recommender import recommend

    return recommend.backfill_covers()


def cmd_purge_dupes(_args) -> int:
    from recommender import recommend

    return recommend.purge_library_dupes()


def cmd_enrich(_args) -> int:
    from recommender import enrich

    return enrich.run()


def cmd_regen_reasons(_args) -> int:
    from recommender import enrich

    return enrich.regen_reasons()


def cmd_jm_login(_args) -> int:
    from recommender import jm_login

    return jm_login.login()


def cmd_feedback(args) -> int:
    from recommender import feedback

    return feedback.record(args.work_id, args.choice)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="E-Hentai 收藏画像与跨站推荐器")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="环境自检")
    c.add_argument("--ai", action="store_true", help="对 DeepSeek 发一次最小请求验证 key")
    c.set_defaults(func=cmd_check)

    sub.add_parser("parse", help="解析 folder_list.txt").set_defaults(func=cmd_parse)

    lk = sub.add_parser("lookup", help="补全 E-Hentai 元数据（长任务，支持断点续跑）")
    lk.add_argument("--limit", type=int, default=None, help="只处理前 N 条（调试用）")
    lk.add_argument("--reset-manual", action="store_true", help="重置人工确认条目后重跑")
    lk.add_argument("--reset-all", action="store_true", help="清空全部元数据后重跑")
    lk.set_defaults(func=cmd_lookup)

    pr = sub.add_parser("profile", help="生成偏好画像")
    pr.set_defaults(func=cmd_profile)

    rc = sub.add_parser("recommend", help="生成推荐报告")
    rc.add_argument("--channel", action="append", choices=["precision", "trending", "series"],
                    help="只跑指定通道（可多次指定）")
    rc.add_argument("--quota", type=int, default=None, help="推荐条数")
    rc.add_argument("--sites", default=None, help="仅使用指定站点（逗号分隔，如 ehentai,nhentai）")
    rc.add_argument("--no-ai", action="store_true", help="禁用 AI 推荐理由")
    rc.add_argument("--force", action="store_true", help="即使 lookup 任务运行中也要执行")
    rc.set_defaults(func=cmd_recommend)

    sc = sub.add_parser("series-check", help="仅跑连载追踪通道")
    sc.add_argument("--quota", type=int, default=None)
    sc.add_argument("--no-ai", action="store_true")
    sc.add_argument("--force", action="store_true")
    sc.set_defaults(func=cmd_series_check)

    st = sub.add_parser("status", help="库/画像/推荐统计")
    st.set_defaults(func=cmd_status)

    up = sub.add_parser("update-profile", help="把反馈折算回画像权重")
    up.set_defaults(func=cmd_update_profile)

    mp = sub.add_parser("manual-pick", help="人工确认条目 manual-pick <行号> [候选下标]")
    mp.add_argument("line_no", type=int)
    mp.add_argument("index", type=int, nargs="?", default=None)
    mp.set_defaults(func=cmd_manual_pick)

    va = sub.add_parser("verify-ai", help="AI 复核全部 AI 命中条目，错配转人工")
    va.add_argument("--max", type=int, default=None, help="最多复核 N 条（调试用）")
    va.set_defaults(func=cmd_verify_ai)

    ar = sub.add_parser("auto-resolve", help="人工条目攻坚：AI 多语言检索 + 扩池 + 复核")
    ar.add_argument("--max", type=int, default=None, help="最多处理 N 条（调试用）")
    ar.set_defaults(func=cmd_auto_resolve)

    sv = sub.add_parser("serve", help="启动本地推荐 UI（浏览器窗口）")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    sv.set_defaults(func=cmd_serve)

    ap = sub.add_parser("app", help="独立窗口模式（V1 正式版入口）")
    ap.set_defaults(func=cmd_app)

    bc = sub.add_parser("backfill-covers", help="为已有推荐补封面 URL")
    bc.set_defaults(func=cmd_backfill_covers)

    pd = sub.add_parser("purge-dupes", help="清理推荐中与收藏库重复的条目")
    pd.set_defaults(func=cmd_purge_dupes)

    en = sub.add_parser("enrich", help="为推荐回填收藏数与简介")
    en.set_defaults(func=cmd_enrich)

    rr = sub.add_parser("regen-reasons", help="用鉴赏式短文重新生成推荐理由")
    rr.set_defaults(func=cmd_regen_reasons)

    jl = sub.add_parser("jm-login", help="18comic 浏览器登录（手动过 Cloudflare 真人验证）")
    jl.set_defaults(func=cmd_jm_login)

    f = sub.add_parser("feedback", help="记录反馈 feedback <ID> like|meh|hide")
    f.add_argument("work_id")
    f.add_argument("choice", choices=["like", "meh", "hide"])
    f.set_defaults(func=cmd_feedback)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
