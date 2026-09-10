# -*- coding: utf-8 -*-
"""核心逻辑离线单元测试（不触发真实网络）。"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from recommender.eh_lookup import _title_sim, score_meta
from recommender.parser import is_generic_title, kana_to_romaji, norm_key, parse_line
from recommender.recommend import (_candidate_identity, _dedupe, _filter_violation,
                                    _filtered, _template_reason, score_work)
from recommender.report import render_html, render_md
from recommender.sites.base import Work


class TestParser(unittest.TestCase):
    def test_basic_fields(self):
        e = parse_line("[堀博昭] しよっか?破滅SEX? [中国翻訳] [DL版]", 1)
        self.assertEqual(e["circle"], "堀博昭")
        self.assertEqual(e["lang"], "zh")
        self.assertTrue(e["digital"])
        self.assertFalse(e["uncensored"])
        self.assertIn("しよっか", e["title_clean"])

    def test_circle_artist_split(self):
        e = parse_line("[蒼夏荘 (蒼夏酢)] 継母堕天?ママハハダテン? [中国翻訳]", 2)
        self.assertEqual(e["circle"], "蒼夏荘")
        self.assertEqual(e["artist"], "蒼夏酢")
        self.assertEqual(e["lang"], "zh")

    def test_uncensored_no_space(self):
        e = parse_line("[gonza]恋する美熟女たち[無修正]", 3)
        self.assertTrue(e["uncensored"])
        self.assertEqual(e["title_clean"], "恋する美熟女たち")

    def test_translator_group(self):
        e = parse_line("[たけあき学] 同級生はドMメイド ?ご主人様? [中国翻訳] [DL版] [甜族星人赞助汉化]", 4)
        self.assertEqual(e["translator_group"], "甜族星人赞助汉化")

    def test_volume_marker(self):
        e = parse_line("[gonza] 命运 1-80 END [Chinese]", 5)
        self.assertEqual(e["volume_marker"], "1-80")

    def test_bilingual_title(self):
        e = parse_line("[Fuyu Mikan] Echi Echi School Life   好色發情學園性生活 [Chinese] [Digital]", 6)
        self.assertIn("Echi Echi School Life", e["title_clean"])

    def test_norm_key(self):
        self.assertEqual(norm_key("ブラッド♡ランチ"), "ぶらっどらんち")
        self.assertEqual(norm_key("Bitch Bitch"), "bitchbitch")
        self.assertEqual(norm_key("全角１２３!"), "全角123")
        # 繁简归一
        self.assertEqual(norm_key("後宮邪教"), norm_key("后宫邪教"))

    def test_kana_to_romaji(self):
        self.assertEqual(kana_to_romaji("びっちびっち"), "bicchibicchi")
        self.assertEqual(kana_to_romaji("きゃら"), "kyara")

    def test_generic_title_filter(self):
        for t in ("第一卷", "第三卷", "第12話", "上卷", "近期汉化合集", "汉化合集", "合集", "短篇", "未命名"):
            self.assertTrue(is_generic_title(t), t)
        for t in ("溺愛観察日記", "Bitch Bitch", "Fanbox汉化合集 (ブルーアーカイブ)", "性活週間"):
            self.assertFalse(is_generic_title(t), t)


class TestMatching(unittest.TestCase):
    def _entry(self, title, circle=None, lang="zh"):
        from recommender.parser import clean_text

        return {
            "title_clean": clean_text(title),
            "circle_clean": circle,
            "artist": None,
            "lang": lang,
            "series_hint": f"{circle or ''}|None|{clean_text(title)}",
        }

    def test_romaji_bridge(self):
        entry = self._entry("びっちびっち", "ぷよちゃ")
        s = _title_sim(entry, "[Puyocha] Bitch Bitch | 淫蕩無極限 [Chinese] [Decensored]")
        self.assertGreater(s, 0.3)

    def test_pykakasi_bridge(self):
        entry = self._entry("同級生はドMメイド ご主人様、エッチなご奉仕教えてください", "たけあき学")
        s = _title_sim(entry, "[Takeakigaku] Doukyuusei wa Do-M Maid ~Goshujin-sama, Ecchi na Gohoushi Oshiete Kudasai~ [Chinese]")
        self.assertGreater(s, 0.7)

    def test_prefer_chinese_version(self):
        entry = self._entry("びっちびっち", "ぷよちゃ")
        meta_ch = {"title_en": "[Puyocha] Bitch Bitch | 淫蕩無極限 [Chinese] [Decensored] [Digital]", "title_jp": ""}
        meta_kr = {"title_en": "[Puyocha] Bitch Bitch ch.1 [Korean]", "title_jp": ""}
        self.assertGreater(score_meta(entry, meta_ch), score_meta(entry, meta_kr))


class TestRecommend(unittest.TestCase):
    PROFILE = {
        "authors": [{"name": "gonza", "count": 6, "weight": 1.0},
                    {"name": "kakao", "count": 2, "weight": 0.4}],
        "tags": [{"tag": "female:milf", "count": 9, "weight": 1.0},
                 {"tag": "female:big breasts", "count": 3, "weight": 0.6}],
        "pref": {"author_hit_weight": 20.0, "tag_hit_weight": 3.0, "chinese_bonus": 5.0,
                 "page_bucket_weights": {"<=30": 0.5, "31-80": 1.0, "81-200": 1.0,
                                         "201-400": 0.6, ">400": 0.4}},
    }

    def _work(self, author, tags, chinese=False, pages=100, rating=4.5):
        return Work(site="e-hentai", work_id="123", title=f"[{author}] Test Work",
                    author=author, tags=tags, rating=rating, rating_count=0,
                    pages=pages, is_chinese=chinese, url="https://e-hentai.org/g/123/x/")

    def test_score_author_hit(self):
        s, facts = score_work(self._work("gonza", []), self.PROFILE)
        self.assertGreater(s, 20.0)
        self.assertEqual(facts["author_hit"]["name"], "gonza")

    def test_score_tag_and_chinese(self):
        s1, _ = score_work(self._work("nobody", ["female:milf"], chinese=False), self.PROFILE)
        s2, _ = score_work(self._work("nobody", ["female:milf"], chinese=True), self.PROFILE)
        self.assertGreater(s2, s1 + 4.0)

    def test_candidate_identity(self):
        w = self._work("gonza", [])
        full, idents = _candidate_identity(w)
        self.assertEqual(full, norm_key("[gonza] Test Work"))
        self.assertIn((norm_key("gonza"), norm_key("Test Work")), idents)

    def test_dedupe_merges_same_work(self):
        w1 = self._work("gonza", [])
        w2 = Work(site="wnacg", work_id="9", title="[gonza] Test Work [中文]",
                  author="gonza", is_chinese=True, url="https://x/9")
        out = _dedupe([(10.0, w1, {}), (8.0, w2, {})])
        self.assertEqual(len(out), 1)
        merged = out[0][1]
        self.assertTrue(merged.is_chinese)
        self.assertEqual(len(out[0][2]["alt_links"]), 1)

    def test_template_reason(self):
        w = self._work("gonza", [])
        r = _template_reason(w, {"author_hit": {"name": "gonza", "count": 6, "weight": 1.0},
                                 "tags_hit": ["female:milf（你收藏 9 次）"]}, "precision")
        self.assertIn("gonza", r)
        self.assertIn("female:milf", r)

    def test_filtered_by_recommended(self):
        w = self._work("gonza", [])
        self.assertTrue(_filtered(w, set(), set(), set(), {"e-hentai:123"}))
        self.assertFalse(_filtered(w, set(), set(), set(), set()))
        self.assertTrue(_filtered(w, {norm_key("[gonza] Test Work")}, set(), set(), set()))
        self.assertTrue(_filtered(w, set(), set(), {"123"}, set()))
        # 同名不同作者不应被过滤
        other = Work(site="e-hentai", work_id="456", title="[kakao] Test Work",
                     author="kakao", url="x")
        self.assertFalse(_filtered(other, {norm_key("[gonza] Test Work")},
                                   {(norm_key("gonza"), norm_key("Test Work"))}, set(), set()))

    def test_dedupe_does_not_merge_same_title_diff_author(self):
        w1 = self._work("gonza", [])
        w2 = Work(site="e-hentai", work_id="456", title="[kakao] Test Work",
                  author="kakao", url="x")
        out = _dedupe([(10.0, w1, {}), (9.0, w2, {})])
        self.assertEqual(len(out), 2)

    def test_hard_filters(self):
        w = Work(site="e-hentai", work_id="1", title="X", tags=[], is_chinese=False)
        self.assertEqual(_filter_violation(w), "无中文版")
        w.is_chinese = True
        w.category = "Cosplay"
        self.assertEqual(_filter_violation(w), "品类:Cosplay")
        w.category = "Manga"
        w.tags = ["other:ai generated"]
        self.assertEqual(_filter_violation(w), "命中排除标签")
        w.tags = []
        w.title = "Some 3D Render"
        self.assertEqual(_filter_violation(w), r"命中模式:\b3d\b")
        w.title = "Normal Title"
        self.assertIsNone(_filter_violation(w))

    def test_ntr_penalty(self):
        w1 = self._work("gonza", [])
        w2 = self._work("gonza", ["male:netorare", "mixed:mmf threesome"])
        s1, _ = score_work(w1, self.PROFILE)
        s2, facts = score_work(w2, self.PROFILE)
        self.assertLess(s2, s1 - 25.0)
        self.assertIn("penalties", facts)


class TestReport(unittest.TestCase):
    def test_render(self):
        items = [
            {
                "work_id": "e-hentai:1", "title": "测试作品", "author": "作者A",
                "tags": ["female:big breasts"], "rating": 4.5, "rating_count": 100,
                "pages": 200, "is_chinese": True, "channel": "precision", "score": 12.0,
                "reason": "测试理由", "links": [{"site": "e-hentai", "url": "https://e-hentai.org/g/1/abc/"}],
            }
        ]
        info = {"time": "t", "channels": ["precision"], "quota": 1, "ai_reason": False,
                "ai_calls": 0, "sites": ["e-hentai"], "profile_works": 10}
        md = render_md(items, info)
        html = render_html(items, info)
        self.assertIn("测试理由", md)
        self.assertIn("card", html)


if __name__ == "__main__":
    unittest.main()
