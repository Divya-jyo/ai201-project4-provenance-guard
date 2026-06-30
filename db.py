"""
SQLite-backed storage + structured audit log for Provenance Guard.

Single table is enough for this project's scope: each row is both the
"current state" of a submission AND its audit trail entry. A real system
would split these (append-only log vs. mutable current-state table); see
planning.md "Anticipated edge cases" / README "Known limitations" for why
that's a known simplification here.
"""

import sqlite3
import json
from datetime import datetime, timezone

DB_PATH = "provenance_guard.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            content_id TEXT PRIMARY KEY,
            creator_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            text TEXT NOT NULL,
            llm_score REAL,
            llm_reasoning TEXT,
            stylometric_score REAL,
            stylometric_detail TEXT,
            combined_score REAL,
            attribution TEXT,
            confidence REAL,
            label TEXT,
            status TEXT NOT NULL DEFAULT 'classified',
            appeal_reasoning TEXT,
            appeal_timestamp TEXT
        )
    """)
    conn.commit()
    conn.close()


def insert_submission(row: dict):
    conn = get_conn()
    conn.execute("""
        INSERT INTO submissions (
            content_id, creator_id, timestamp, text,
            llm_score, llm_reasoning, stylometric_score, stylometric_detail,
            combined_score, attribution, confidence, label, status
        ) VALUES (
            :content_id, :creator_id, :timestamp, :text,
            :llm_score, :llm_reasoning, :stylometric_score, :stylometric_detail,
            :combined_score, :attribution, :confidence, :label, :status
        )
    """, row)
    conn.commit()
    conn.close()


def get_submission(content_id: str):
    conn = get_conn()
    cur = conn.execute("SELECT * FROM submissions WHERE content_id = ?", (content_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def file_appeal(content_id: str, creator_reasoning: str) -> bool:
    conn = get_conn()
    cur = conn.execute("""
        UPDATE submissions
        SET status = 'under_review',
            appeal_reasoning = ?,
            appeal_timestamp = ?
        WHERE content_id = ?
    """, (creator_reasoning, datetime.now(timezone.utc).isoformat(), content_id))
    conn.commit()
    updated = cur.rowcount > 0
    conn.close()
    return updated


def get_log(limit: int = 50):
    conn = get_conn()
    cur = conn.execute("""
        SELECT * FROM submissions ORDER BY timestamp DESC LIMIT ?
    """, (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def now_iso():
    return datetime.now(timezone.utc).isoformat()
