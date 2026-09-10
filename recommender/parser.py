# -*- coding: utf-8 -*-
"""解析 folder_list.txt（GBK）→ 结构化条目 + 标题规范化。"""
from __future__ import annotations

import re
import unicodedata

LANG_ZH = {"中国翻訳", "中国語", "Chinese", "中文", "简体", "繁體", "国语", "中譯", "中译", "简中", "繁中"}
LANG_JA = {"日本語", "Japanese"}
LANG_EN = {"English"}
UNCENSORED = {"無修正", "无修正", "Uncensored", "無碼", "无码"}
DIGITAL = {"DL版", "Digital", "DL", "電子版", "电子版"}
GROUP_HINTS = ("汉化组", "漢化組", "汉化", "漢化", "翻譯", "翻译", "製作", "制作", "赞助", "机翻", "組", "组", "字幕组", "出品")
VOL_RE = re.compile(
    r"((?:\d{1,3})\s*[-~～]\s*(?:\d{1,3}))\s*(?:END)?|第\s*[0-9０-９一二三四五六七八九十百]+\s*[巻卷話话冊册]|Vol\.?\s*\d+|[Cc]h?\s*\d+\s*-\s*\d+|Zenpen|Chuuhen|Kouhen",
    re.IGNORECASE,
)
TRAILING_BRACKET_RE = re.compile(r"\[([^\[\]]*)\]\s*$")
LEADING_BRACKET_RE = re.compile(r"^\[([^\[\]]+)\]")

_BASIC_KANA = {
    "あ": "a", "い": "i", "う": "u", "え": "e", "お": "o",
    "か": "ka", "き": "ki", "く": "ku", "け": "ke", "こ": "ko",
    "さ": "sa", "し": "shi", "す": "su", "せ": "se", "そ": "so",
    "た": "ta", "ち": "chi", "つ": "tsu", "て": "te", "と": "to",
    "な": "na", "に": "ni", "ぬ": "nu", "ね": "ne", "の": "no",
    "は": "ha", "ひ": "hi", "ふ": "fu", "へ": "he", "ほ": "ho",
    "ま": "ma", "み": "mi", "む": "mu", "め": "me", "も": "mo",
    "や": "ya", "ゆ": "yu", "よ": "yo",
    "ら": "ra", "り": "ri", "る": "ru", "れ": "re", "ろ": "ro",
    "わ": "wa", "を": "wo", "ん": "n",
    "が": "ga", "ぎ": "gi", "ぐ": "gu", "げ": "ge", "ご": "go",
    "ざ": "za", "じ": "ji", "ず": "zu", "ぜ": "ze", "ぞ": "zo",
    "だ": "da", "ぢ": "ji", "づ": "zu", "で": "de", "ど": "do",
    "ば": "ba", "び": "bi", "ぶ": "bu", "べ": "be", "ぼ": "bo",
    "ぱ": "pa", "ぴ": "pi", "ぷ": "pu", "ぺ": "pe", "ぽ": "po",
    "ぁ": "a", "ぃ": "i", "ぅ": "u", "ぇ": "e", "ぉ": "o",
    "ゃ": "ya", "ゅ": "yu", "ょ": "yo", "ゔ": "vu",
}


def _kata_to_hira(ch: str) -> str:
    o = ord(ch)
    return chr(o - 0x60) if 0x30A1 <= o <= 0x30F6 else ch


def kana_to_romaji(text: str) -> str:
    """假名 → 罗马音（hepburn 近似，含促音/拗音；汉字与符号原样保留）。"""
    s = unicodedata.normalize("NFKC", text or "")
    chars = [_kata_to_hira(c) for c in s]
    out: list[str] = []
    i = 0
    while i < len(chars):
        ch = chars[i]
        if ch == "っ" and i + 1 < len(chars) and chars[i + 1] in _BASIC_KANA:
            nxt = _BASIC_KANA[chars[i + 1]]
            if out and out[-1] and out[-1][-1] in "aiueo" and nxt and nxt[0] not in "aiueon":
                pass
            out.append(nxt[0])  # 促音：重复下一个音的首辅音
        elif ch in _BASIC_KANA:
            r = _BASIC_KANA[ch]
            if ch in ("ゃ", "ゅ", "ょ") and out and out[-1] and out[-1][-1] in "iu":
                out[-1] = out[-1][:-1] + r  # 拗音：きゃ → kya
            else:
                out.append(r)
        elif ch == "ー":
            pass
        else:
            out.append(ch)
        i += 1
    return "".join(out)


_KKS = None
_KANA_RE = re.compile(r"[\u3040-\u30ff]")


def to_romaji(text: str) -> str:
    """完整罗马音化（汉字+假名 → hepburn，pykakasi）；无 pykakasi 时退回假名转换。

    仅对含假名的文本使用；纯中文标题直接返回空串（避免日语音读误伤汉字）。
    """
    text = text or ""
    if not _KANA_RE.search(text):
        return ""
    global _KKS
    if _KKS is None:
        try:
            import pykakasi

            _KKS = pykakasi.kakasi()
        except Exception:  # noqa: BLE001
            _KKS = False
    if _KKS is False:
        return kana_to_romaji(text)
    try:
        return "".join(item["hepburn"] for item in _KKS.convert(text))
    except Exception:  # noqa: BLE001
        return kana_to_romaji(text)


_OPENCC = None


def _t2s(s: str) -> str:
    """繁体 → 简体（用于跨站点标题去重；转换失败时原样返回）。"""
    global _OPENCC
    if not any("\u4e00" <= ch <= "\u9fff" for ch in s):
        return s
    if _OPENCC is None:
        try:
            from opencc import OpenCC

            _OPENCC = OpenCC("t2s")
        except Exception:  # noqa: BLE001
            _OPENCC = False
    if _OPENCC is False:
        return s
    try:
        return _OPENCC.convert(s)
    except Exception:  # noqa: BLE001
        return s


def norm_key(s: str) -> str:
    """规范化 key：NFKC、小写、片假名→平假名、繁体→简体、去空白/标点/符号。"""
    s = unicodedata.normalize("NFKC", s or "")
    s = s.lower()
    s = _t2s(s)
    out: list[str] = []
    for ch in s:
        o = ord(ch)
        if 0x30A1 <= o <= 0x30F6:  # 片假名 → 平假名
            ch = chr(o - 0x60)
        cat = unicodedata.category(ch)
        if ch.isspace() or cat.startswith("P") or cat.startswith("S"):
            continue
        out.append(ch)
    return "".join(out)


def clean_text(s: str) -> str:
    """清洗：把编码丢失产生的 ? 去掉、压缩空白。"""
    s = (s or "").replace("?", "")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def classify_group(grp: str) -> tuple[str, str | bool]:
    grp = grp.strip()
    if grp in LANG_ZH:
        return ("lang", "zh")
    if grp in LANG_JA:
        return ("lang", "ja")
    if grp in LANG_EN:
        return ("lang", "en")
    if grp in UNCENSORED:
        return ("uncensored", True)
    if grp in DIGITAL:
        return ("digital", True)
    if any(h in grp for h in GROUP_HINTS):
        return ("translator", grp)
    return ("extra", grp)


def parse_line(line: str, line_no: int) -> dict | None:
    raw = line.strip()
    if not raw:
        return None

    circle = artist = None
    rest = raw
    m = LEADING_BRACKET_RE.match(rest)
    if m:
        head = m.group(1).strip()
        rest = rest[m.end():].strip()
        if "(" in head and head.endswith(")"):
            circle, _, artist = head.rpartition("(")
            artist = artist.rstrip(")").strip()
            circle = circle.strip()
        else:
            circle = head

    lang = None
    uncensored = False
    digital = False
    translator = None
    extras: list[str] = []
    while True:
        m = TRAILING_BRACKET_RE.search(rest)
        if not m:
            break
        kind, value = classify_group(m.group(1))
        if kind == "lang" and lang is None:
            lang = str(value)
        elif kind == "uncensored":
            uncensored = True
        elif kind == "digital":
            digital = True
        elif kind == "translator" and translator is None:
            translator = str(value)
        elif kind == "extra":
            extras.append(str(value))
        rest = rest[: m.start()].rstrip()

    title = rest.strip()
    title_clean = clean_text(title)
    if not title_clean:
        return None

    # 语言推断：汉化组等标记里带"汉化/中文"的视为中文
    if lang is None:
        for g in ([translator] if translator else []) + extras:
            if re.search(r"汉化|漢化|中文|中国|Chinese", g or ""):
                lang = "zh"
                break

    vol_m = VOL_RE.search(title_clean)
    if vol_m:
        volume_marker = vol_m.group(1) or vol_m.group(0)
        series_title = title_clean.replace(vol_m.group(0), "").strip(" -_~～()[] ")
    else:
        volume_marker = None
        series_title = title_clean
    series_hint = norm_key(f"{circle}|{artist}|{series_title}")

    return {
        "line_no": line_no,
        "raw": raw,
        "circle": circle,
        "circle_clean": clean_text(circle) if circle else None,
        "artist": artist,
        "title": title,
        "title_clean": title_clean,
        "lang": lang,
        "uncensored": uncensored,
        "digital": digital,
        "translator_group": translator,
        "extra_notes": extras,
        "volume_marker": volume_marker,
        "canonical_key": norm_key(f"{circle}|{artist}|{title_clean}"),
        "series_hint": series_hint,
    }


def extract_author_from_title(title: str) -> str:
    """从 EH/wnacg 风格标题的 [Circle (Artist)] 或 [Circle] 前缀提取作者名。"""
    m = re.match(r"^\[([^\[\]]+)\]", (title or "").strip())
    if not m:
        return ""
    head = m.group(1)
    if "(" in head and head.endswith(")"):
        _, _, artist = head.rpartition("(")
        return artist.rstrip(")").strip()
    return head


_GENERIC_TITLE_RE = re.compile(
    r"^(?:"
    r"第?[0-9０-９一二三四五六七八九十百]+[卷集册話话部]"          # 第一卷 / 3卷 / 第12話
    r"|(?:上|中|下|全)[卷集册]"                                    # 上卷
    r"|(?:近期|最新|新作|个人)?(?:汉化|漢化)?(?:合集|合辑|整理)"    # 近期汉化合集
    r"|(?:汉化|漢化)(?:合集|合辑)?"
    r"|(?:合集|合辑|合刊)"
    r"|短篇|中篇|長篇|长篇|单行本|単行本|雜圖|杂图|插画|插畫|未命名|无标题|無題"
    r")$"
)


def is_generic_title(title_clean: str) -> bool:
    """是否为明显不是作品名的通用标题（如"第一卷"“近期汉化合集”）。"""
    t = (title_clean or "").strip()
    return bool(t) and len(t) <= 8 and bool(_GENERIC_TITLE_RE.match(t))


def parse_file(path) -> tuple[list[dict], list[dict]]:
    """读取 GBK 文件并解析。返回 (entries, failures)。"""
    text = path.read_text(encoding="gbk", errors="replace")
    entries: list[dict] = []
    failures: list[dict] = []
    for i, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        entry = parse_line(line, i)
        if entry is None:
            failures.append({"line_no": i, "raw": line.strip(), "reason": "无法解析（标题为空）"})
        else:
            entries.append(entry)
    return entries, failures
