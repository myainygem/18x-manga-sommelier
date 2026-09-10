# -*- coding: utf-8 -*-
"""推荐器离线冒烟测试：假适配器 → 全流程（过滤/打分/报告/入库/去重）。
用法: python tests/smoke_recommend.py（不触发真实网络；完成后自动清理假数据）
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from recommender import recommend
from recommender.sites.base import SiteAdapter, Work


class FakeAdapter(SiteAdapter):
    name = "fake"

    def available(self) -> bool:
        return True

    def search_author(self, author: str, limit: int = 8) -> list[Work]:
        return [
            Work(
                site=self.name, work_id=f"{author}-1",
                title=f"[{author}] Fake New Work [Chinese]", author=author,
                tags=["female:big breasts", "male:milf"],
                rating=4.6, rating_count=80, pages=190,
                url=f"https://example.com/{author}-1", is_chinese=True,
            )
        ]

    def search(self, query: str, limit: int = 15) -> list[Work]:
        return []

    def trending(self, limit: int = 10) -> list[Work]:
        return []


def main() -> int:
    recommend.available_adapters = lambda: [FakeAdapter()]

    from recommender.db import get_conn

    with get_conn() as conn:  # 幂等：清理历史假数据
        conn.execute("DELETE FROM recommended WHERE primary_site='fake'")
        conn.commit()

    print("== 第一次运行 ==")
    rc = recommend.run(channels=["precision"], quota=3, no_ai=True, force=True, verbose=False)
    assert rc == 0, f"第一次运行失败: {rc}"

    from recommender.config import ROOT
    from recommender.db import get_conn

    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM recommended WHERE primary_site='fake' ORDER BY score DESC")]
    print(f"入库推荐: {len(rows)} 条")
    assert len(rows) == 3, f"期望 3 条, 实际 {len(rows)}"
    for r in rows:
        assert r["reason"], "推荐理由为空"
        assert r["links"], "链接为空"
    print("  首条:", rows[0]["title"], "| 分:", rows[0]["score"], "| 理由:", rows[0]["reason"][:40])

    reports = sorted((ROOT / "reports").glob("recommendation_*.html"))
    assert reports, "未生成报告"
    print("报告文件:", reports[-1].name, reports[-1].stat().st_size, "bytes")

    print("== 后续运行（验证历史推荐抑制：每轮推下一批，不重复）==")
    from recommender.db import get_conn
    from recommender.recommend import load_profile

    n_cands = len(load_profile()["authors"][:12])

    seen_total = set()
    rounds = 0
    while rounds < 12:
        rc = recommend.run(channels=["precision"], quota=3, no_ai=True, force=True, verbose=False)
        assert rc == 0
        with get_conn() as conn:
            rows = [r[0] for r in conn.execute(
                "SELECT work_id FROM recommended WHERE primary_site='fake'")]
        new_total = len(rows)
        if new_total == len(seen_total):
            break  # 没有新增 = 全部候选已推过
        seen_total = set(rows)
        rounds += 1
    assert len(seen_total) == n_cands, f"期望 {n_cands} 个候选各推一次, 实际 {len(seen_total)}"
    print(f"循环 {rounds} 轮推完 {n_cands} 个候选（+首跑 3 条），无重复 ✓")

    print("== 清理假数据 ==")
    with get_conn() as conn:
        conn.execute("DELETE FROM recommended WHERE primary_site='fake'")
        conn.commit()
    print("冒烟测试通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
