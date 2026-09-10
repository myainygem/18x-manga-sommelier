# -*- coding: utf-8 -*-
"""DeepSeek AI 增强层：搜索词扩展 / 标题消歧 / 推荐理由撰写。

设计原则：AI 只做"判断与表达"，不做"事实"。
- 所有函数失败/关闭/无 key 时返回 None，调用方回退到规则实现；
- 输出一律要求 JSON 并经校验，禁止 AI 编造输入之外的事实。
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from .config import config
from .db import get_conn
from .http_client import HttpClient


class AIClient:
    def __init__(self) -> None:
        self.ai = config["ai"]
        self.key: str = self.ai.get("api_key") or ""
        self.base_url: str = self.ai.get("base_url", "https://api.deepseek.com").rstrip("/")
        self.model: str = self.ai.get("model", "deepseek-chat")
        self.max_calls: int = int(self.ai.get("max_calls_per_run", 200))
        self.max_tokens: int = int(self.ai.get("max_tokens_per_call", 800))
        self.temperature: float = float(self.ai.get("temperature", 0.3))
        self.calls: int = 0
        self.http = HttpClient()

    @property
    def available(self) -> bool:
        return bool(self.key) and bool(self.ai.get("enabled", True)) and self.calls < self.max_calls

    def enabled(self, feature: str) -> bool:
        return bool(self.ai.get(feature, True))

    def _chat(self, system: str, user: str, feature: str = "chat") -> str | None:
        if not self.available:
            return None
        # DeepSeek 要求 prompt 中出现 "json" 一词才允许 json_object 响应格式
        system = f"{system}\n（约束：只输出 JSON 对象，不要输出任何其他内容。）"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
        }
        try:
            r = self.http.post_json(
                f"{self.base_url}/chat/completions",
                "api.deepseek.com",
                payload,
                headers={"Authorization": f"Bearer {self.key}"},
            )
            data = r.json()
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            self.calls += 1
            self._record_usage(
                usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), feature
            )
            return content
        except Exception:  # noqa: BLE001 —— 任何失败都静默降级
            return None

    def _record_usage(self, prompt_tokens: int, completion_tokens: int, feature: str) -> None:
        try:
            with get_conn() as conn:
                conn.execute(
                    "INSERT INTO ai_usage (ts, feature, model, prompt_tokens, completion_tokens) "
                    "VALUES (?,?,?,?,?)",
                    (
                        datetime.now().isoformat(timespec="seconds"),
                        feature,
                        self.model,
                        prompt_tokens,
                        completion_tokens,
                    ),
                )
        except Exception:  # noqa: BLE001
            pass

    def chat_json(self, system: str, user: str, feature: str, validator=None):
        content = self._chat(system, user, feature)
        if content is None:
            return None
        obj = None
        try:
            obj = json.loads(content)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", content, re.S)
            if m:
                try:
                    obj = json.loads(m.group(0))
                except json.JSONDecodeError:
                    obj = None
        if obj is None:
            return None
        if validator is not None and not validator(obj):
            return None
        return obj

    # ---- 三个功能 ----

    def expand_queries(self, circle: str, artist: str, title: str) -> list[str] | None:
        """生成用于站点搜索的多语言查询词（日文原名/罗马音/中文译名/短关键词）。"""
        user = json.dumps(
            {"circle": circle or "", "artist": artist or "", "title": title or ""},
            ensure_ascii=False,
        )
        system = (
            "你是漫画多语言搜索词生成助手。根据给定的社团/作者/标题，生成用于在各漫画站搜索的查询词。"
            "要求：给出日文原名、罗马音、中文译名（若可合理推断）、去除特殊符号的短关键词等变体。"
            "只输出 JSON：{\"queries\": [\"...\", ...]}，最多 8 个查询词。"
            "禁止编造与输入无关的内容；无法推断的变体就不要写。"
        )
        return self.chat_json(
            system,
            user + "\n输出 JSON。",
            "search_expand",
            validator=lambda o: isinstance(o.get("queries"), list) and len(o["queries"]) > 0,
        )

    def advanced_queries(self, circle: str, artist: str, title: str, translator: str = "") -> list[str] | None:
        """攻坚模式：为难以匹配的条目生成多语言检索词（含日文原名还原/罗马音/英译/汉化组组合）。"""
        user = json.dumps(
            {
                "circle": circle or "",
                "artist": artist or "",
                "title": title or "",
                "汉化组": translator or "",
            },
            ensure_ascii=False,
        )
        system = (
            "你是漫画检索专家。为给定的漫画生成用于 E-Hentai 站内搜索的查询词，"
            "目标是把同一部作品的任意语言版本搜出来。"
            "请生成：日文原名（若给定标题是中文译名，请还原最可能的日文原名）、罗马音、英文译名、"
            "去掉特殊符号的短关键词、以及「汉化组名+关键词」的组合（若有汉化组）。"
            "最多 10 个查询词，按预期命中率从高到低排序。"
            "只输出 JSON：{\"queries\": [\"...\", ...]}。禁止编造与输入无关的内容；"
            "不确定的还原就不要写。"
        )
        return self.chat_json(
            system,
            user + "\n输出 JSON。",
            "search_expand",
            validator=lambda o: isinstance(o.get("queries"), list) and len(o["queries"]) > 0,
        )

    def disambiguate(self, entry: dict, candidates: list[dict]) -> dict | None:
        """从候选中选出最可能是用户收藏的那一部。"""
        user = json.dumps(
            {"entry": entry, "candidates": candidates}, ensure_ascii=False
        )
        system = (
            "你是漫画元数据匹配助手。用户有一个收藏文件夹名，下面是站点搜索返回的候选作品。"
            "请选出最可能是同一部作品的候选（注意语言版本差异：中文翻译版与日文原版视为同一部作品）。"
            "严格要求：同一社团/作者的其他作品不算同一部；标题必须对应同一内容；"
            "如果候选列表中没有同一部作品，必须输出 index=-1，宁可无匹配也不要选错。"
            "只输出 JSON：{\"index\": 候选下标, \"confidence\": 0到1的小数, \"reason\": \"一句话\"}；"
            "若都不匹配输出 {\"index\": -1, \"confidence\": 0, \"reason\": \"无匹配\"}。"
            "只能从给定候选中选择，禁止编造候选之外的任何信息。"
        )
        return self.chat_json(
            system,
            user + "\n输出 JSON。",
            "disambiguate",
            validator=lambda o: isinstance(o.get("index"), int)
            and (o["index"] == -1 or 0 <= o["index"] < len(candidates)),
        )

    def verify_match(self, entry: dict, candidate_title: str) -> dict | None:
        """复核：用户收藏与站点作品是否为同一部（防止同社团其他作品被误配）。"""
        user = json.dumps(
            {
                "用户收藏": {
                    "社团": entry.get("circle_clean") or "",
                    "作者": entry.get("artist") or "",
                    "标题": entry.get("title_clean") or "",
                },
                "站点作品标题": candidate_title or "",
            },
            ensure_ascii=False,
        )
        system = (
            "你是漫画元数据核对助手。判断用户收藏的作品与站点上的作品是否为同一部作品。"
            "注意：同一社团/作者的其他作品不算同一部；标题必须对应同一内容；"
            "不同语言版本（中文翻译/日文原版/英文版/无修版）视为同一部。"
            "只输出 JSON：{\"same\": true或false, \"reason\": \"一句话\"}。"
        )
        return self.chat_json(
            system,
            user + "\n输出 JSON。",
            "verify",
            validator=lambda o: isinstance(o.get("same"), bool),
        )

    def write_reason(self, facts: dict) -> str | None:
        """基于已核实事实写纯作品点评（简介已单独展示，不复述剧情、不谈私人契合）。"""
        user = json.dumps(facts, ensure_ascii=False)
        system = (
            "你是资深成人漫画评论家。基于给定的事实数据，用简体中文写一篇 110-170 字的作品点评。"
            "只讨论作品本身：画风与分镜特点、题材与内容、剧情或设定亮点（可概括但不要复述剧情梗概）、"
            "作者风格特点、评分与收藏人数等口碑数据、篇幅与阅读建议。"
            "严禁出现：读者的个人收藏情况或收藏次数、口味契合度、汉化/中文便利性、"
            "'值得入手''值得一看'等客套话。禁止编造数据中不存在的事实，缺失的方面绕开。"
            "只输出 JSON：{\"reason\": \"...\"}。"
        )
        result = self.chat_json(
            system,
            user + "\n输出 JSON。",
            "reasons",
            validator=lambda o: isinstance(o.get("reason"), str) and 30 <= len(o["reason"]) <= 400,
        )
        return result.get("reason") if result else None
