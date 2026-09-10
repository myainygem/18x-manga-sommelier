# -*- coding: utf-8 -*-
"""站点适配层：E-Hentai（主）、wnacg（中文）、nhentai/hitomi（备用，自动探测停用）。"""
from __future__ import annotations

from ..config import config
from .base import SiteAdapter, Work
from .ehentai_adapter import EhentaiAdapter
from .hitomi_adapter import HitomiAdapter
from .nhentai_adapter import NhentaiAdapter
from .wnacg_adapter import WnacgAdapter
from .jm18_adapter import JM18Adapter


def get_adapters() -> list[SiteAdapter]:
    adapters: list[SiteAdapter] = []
    if config["sites"].get("ehentai", True):
        adapters.append(EhentaiAdapter())
    if config["sites"].get("wnacg", True):
        adapters.append(WnacgAdapter())
    if config["sites"].get("nhentai", False):
        adapters.append(NhentaiAdapter())
    if config["sites"].get("hitomi", True):
        adapters.append(HitomiAdapter())
    if config["sites"].get("18comic", False):
        adapters.append(JM18Adapter())
    return adapters


def available_adapters() -> list[SiteAdapter]:
    out = []
    for a in get_adapters():
        if a.available():
            out.append(a)
        else:
            print(f"[适配器] {a.name}: 不可用（{a.reason_unavailable or '探测失败'}）")
    return out


__all__ = ["SiteAdapter", "Work", "get_adapters", "available_adapters"]
