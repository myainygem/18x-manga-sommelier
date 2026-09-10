# -*- coding: utf-8 -*-
"""SQLite 存储：解析条目、元数据、推荐与反馈、别名、AI 用量。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .config import ROOT

DB_PATH = ROOT / "data" / "library.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    line_no INTEGER,
    raw TEXT,
    circle TEXT,
    circle_clean TEXT,
    artist TEXT,
    title TEXT,
    title_clean TEXT,
    lang TEXT,
    uncensored INTEGER DEFAULT 0,
    digital INTEGER DEFAULT 0,
    translator_group TEXT,
    extra_notes TEXT,
    volume_marker TEXT,
    canonical_key TEXT,
    series_hint TEXT
);
CREATE TABLE IF NOT EXISTS eh_metadata (
    entry_id INTEGER PRIMARY KEY,
    gid INTEGER,
    token TEXT,
    title_en TEXT,
    title_jp TEXT,
    category TEXT,
    lang TEXT,
    pages INTEGER,
    rating REAL,
    rating_count INTEGER,
    uploader TEXT,
    posted INTEGER,
    tags TEXT,
    parent_gid INTEGER,
    parent_key TEXT,
    chosen_by TEXT,
    confidence REAL,
    raw_json_path TEXT
);
CREATE TABLE IF NOT EXISTS recommended (
    work_id TEXT PRIMARY KEY,
    primary_site TEXT,
    title TEXT,
    author TEXT,
    tags TEXT,
    rating REAL,
    rating_count INTEGER,
    pages INTEGER,
    lang TEXT,
    channel TEXT,
    score REAL,
    reason TEXT,
    links TEXT,
    first_seen TEXT,
    feedback TEXT,
    feedback_at TEXT
);
CREATE TABLE IF NOT EXISTS aliases (
    alias_key TEXT PRIMARY KEY,
    work_key TEXT
);
CREATE TABLE IF NOT EXISTS ai_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    feature TEXT,
    model TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER
);
"""


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # 迁移：封面 URL 列
        cols = {r[1] for r in conn.execute("PRAGMA table_info(recommended)")}
        if "cover_url" not in cols:
            conn.execute("ALTER TABLE recommended ADD COLUMN cover_url TEXT")
        # 迁移：反馈是否已折算进画像权重
        if "feedback_applied" not in cols:
            conn.execute("ALTER TABLE recommended ADD COLUMN feedback_applied INTEGER DEFAULT 0")
            # 历史反馈视为已应用（其权重已反映在当前画像中）
            conn.execute("UPDATE recommended SET feedback_applied=1 "
                         "WHERE feedback IN ('like','meh')")
        # 迁移：作品简介
        if "description" not in cols:
            conn.execute("ALTER TABLE recommended ADD COLUMN description TEXT")
        # 迁移：批次淘汰标记（每次更新推荐后，旧批次标记 superseded=1，推荐页只显示最新一批）
        if "superseded" not in cols:
            conn.execute("ALTER TABLE recommended ADD COLUMN superseded INTEGER NOT NULL DEFAULT 0")
    with get_conn() as conn:
        ecols = {r[1] for r in conn.execute("PRAGMA table_info(eh_metadata)")}
        if "removed" not in ecols:
            conn.execute("ALTER TABLE eh_metadata ADD COLUMN removed INTEGER DEFAULT 0")


def upsert_entries(entries: list[dict]) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM entries")
        # 重置自增序列，保证 id 与行顺序一致（1..N），供 eh_metadata 外键引用
        conn.execute("DELETE FROM sqlite_sequence WHERE name='entries'")
        for e in entries:
            conn.execute(
                """INSERT INTO entries (line_no, raw, circle, circle_clean, artist, title,
                    title_clean, lang, uncensored, digital, translator_group, extra_notes,
                    volume_marker, canonical_key, series_hint)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    e.get("line_no"),
                    e.get("raw"),
                    e.get("circle"),
                    e.get("circle_clean"),
                    e.get("artist"),
                    e.get("title"),
                    e.get("title_clean"),
                    e.get("lang"),
                    1 if e.get("uncensored") else 0,
                    1 if e.get("digital") else 0,
                    e.get("translator_group"),
                    json.dumps(e.get("extra_notes", []), ensure_ascii=False),
                    e.get("volume_marker"),
                    e.get("canonical_key"),
                    e.get("series_hint"),
                ),
            )
