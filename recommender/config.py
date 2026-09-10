# -*- coding: utf-8 -*-
"""配置加载：config.json + 环境变量（DEEPSEEK_API_KEY 优先）。

开发模式：ROOT = 项目目录；打包安装版（PyInstaller frozen）：ROOT = %APPDATA%\\MangaRecommender，
数据/配置/报告全部写入该用户目录，并在首次运行时从安装目录播种默认配置与数据。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path


def _resolve_root() -> Path:
    if getattr(sys, "frozen", False):
        # 便携模式：exe 所在目录存在 portable.flag 时，数据/配置全部放在 exe 目录下，
        # 不触碰 C 盘（D:\Soft\ehentai 这类绿色部署）。
        exe_dir = Path(sys.executable).resolve().parent
        if (exe_dir / "portable.flag").exists():
            _seed_from_bundle(exe_dir)
            return exe_dir
        base = Path(os.environ.get("APPDATA") or str(Path.home())) / "MangaRecommender"
        base.mkdir(parents=True, exist_ok=True)
        _seed_from_bundle(base)
        return base
    return Path(__file__).resolve().parent.parent


def _seed_from_bundle(root: Path) -> None:
    """安装版首启：把打包内置的默认 config.json 与数据目录复制到用户目录（已存在则跳过）。"""
    meipass = Path(getattr(sys, "_MEIPASS", ""))
    if not meipass or not meipass.exists():
        return
    try:
        if not (root / "config.json").exists() and (meipass / "config.json").exists():
            shutil.copyfile(meipass / "config.json", root / "config.json")
        data_src = meipass / "data"
        data_dst = root / "data"
        if data_src.exists() and not (data_dst / "library.db").exists():
            data_dst.mkdir(parents=True, exist_ok=True)
            for p in data_src.rglob("*"):
                rel = p.relative_to(data_src)
                dst = data_dst / rel
                if p.is_dir():
                    dst.mkdir(parents=True, exist_ok=True)
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if not dst.exists():
                        shutil.copy2(p, dst)
    except Exception:  # noqa: BLE001 —— 播种失败不影响启动
        pass


ROOT = _resolve_root()
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"

DEFAULTS = {
    "proxy": "",
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "rate_limits": {
        "e-hentai.org": 7,
        "hitomi.la": 2,
        "nhentai.net": 2,
        "wnacg.com": 3,
        "18comic.vip": 3,
        "api.deepseek.com": 1,
    },
    "sites": {"ehentai": True, "hitomi": True, "nhentai": False, "wnacg": True, "18comic": True},
    "ai": {
        "enabled": True,
        "api_key": "",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "temperature": 0.3,
        "max_calls_per_run": 400,
        "max_tokens_per_call": 500,
        "search_expand": True,
        "disambiguate": True,
        "reasons": True,
    },
    "recommend": {
        "quota": 20,
        # 固定配额：连载 0 = 不设上限（按实际找到的数量）
        "quota_precision": 10,
        "quota_trending": 10,
        "quota_series": 0,
        "min_rating_count": 30,
        "top_authors_n": 20,
        "top_tags_n": 40,
        "author_hit_weight": 20.0,
        "chinese_bonus": 5.0,
        "min_score": 5.0,
        # 正式版启用：优先选上次推荐之后时间段的作品
        "prefer_recent_since_last": True,
    },
    "filters": {
        "require_chinese": True,
        "hard_exclude_categories": ["Cosplay", "Asian Porn", "Image Set", "Western", "Non-H",
                                    "寫真", "寫真集", "真人"],
        "hard_exclude_tags": ["other:ai generated"],
        "title_regex_exclude": [r"\b3d\b", r"\bai generated\b", "ai生成", "AI生成"],
        "penalty_tags": {
            "male:netorare": 20.0,
            "mixed:mmf threesome": 10.0,
            "female:netorare": 5.0,
        },
    },
    "lookup": {
        "ehentai_search_url": "https://e-hentai.org/?f_search={q}&f_cats=0",
        "match_confidence_min": 0.55,
        "ai_disambiguate_margin": 0.10,
    },
    "ui": {
        "search_site": "ehentai",
    },
}


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


class Config:
    def __init__(self) -> None:
        self.data = json.loads(json.dumps(DEFAULTS))
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, encoding="utf-8") as f:
                _deep_merge(self.data, json.load(f))
        # API key: 环境变量优先于 config.json
        self.data["ai"]["api_key"] = (
            os.environ.get("DEEPSEEK_API_KEY") or self.data["ai"].get("api_key") or ""
        )

    def __getitem__(self, key: str):
        return self.data[key]

    def get(self, key: str, default=None):
        return self.data.get(key, default)


config = Config()
