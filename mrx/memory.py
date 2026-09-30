"""Persistent storage: long-term memory, chat log, drafts, known devices, scheduled tasks."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY, text TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '', body TEXT NOT NULL, meta TEXT NOT NULL DEFAULT '{}',
    created REAL NOT NULL, status TEXT NOT NULL DEFAULT 'draft'
);
CREATE TABLE IF NOT EXISTS devices (
    mac TEXT PRIMARY KEY, ip TEXT, hostname TEXT, vendor TEXT, kind TEXT,
    label TEXT NOT NULL DEFAULT '', trusted INTEGER NOT NULL DEFAULT 0,
    first_seen REAL NOT NULL, last_seen REAL NOT NULL, info TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, prompt TEXT NOT NULL,
    schedule TEXT NOT NULL, next_run REAL, last_run REAL, last_result TEXT,
    enabled INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL
);
"""

_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(text, tags, content='facts', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
  INSERT INTO facts_fts(rowid, text, tags) VALUES (new.id, new.text, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, text, tags) VALUES ('delete', old.id, old.text, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, text, tags) VALUES ('delete', old.id, old.text, old.tags);
  INSERT INTO facts_fts(rowid, text, tags) VALUES (new.id, new.text, new.tags);
END;
"""


def _words(query: str) -> list[str]:
    # \w is Unicode-aware, so this works for any language's script.
    return [w for w in re.findall(r"\w+", query.lower()) if len(w) > 1]


class Memory:
    def __init__(self, path: Path | str):
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(SCHEMA)
            try:
                self._db.executescript(_FTS)
                self.fts = True
            except sqlite3.OperationalError:
                self.fts = False
            self._db.commit()

    # -- helpers ---------------------------------------------------------
    def _all(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def _insert(self, sql: str, args: tuple = ()) -> int:
        """Run an INSERT; returns the new row id."""
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur.lastrowid

    def _run(self, sql: str, args: tuple = ()) -> int:
        """Run an UPDATE/DELETE/INSERT; returns the number of rows affected."""
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur.rowcount

    # -- long-term facts -------------------------------------------------
    def remember(self, text: str, tags: str = "") -> int:
        now = time.time()
        return self._insert(
            "INSERT INTO facts(text, tags, created, updated) VALUES (?,?,?,?)", (text, tags, now, now)
        )

    def update_fact(self, fact_id: int, text: str) -> bool:
        return self._run("UPDATE facts SET text=?, updated=? WHERE id=?", (text, time.time(), fact_id)) > 0

    def forget(self, fact_id: int) -> bool:
        return self._run("DELETE FROM facts WHERE id=?", (fact_id,)) > 0

    def all_facts(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._all("SELECT id, text, tags, created FROM facts ORDER BY updated DESC LIMIT ?", (limit,))

    def recall(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        words = _words(query)
        if not words:
            return self.all_facts(limit)
        if self.fts:
            match = " OR ".join(f'"{w}"' for w in words)
            try:
                return self._all(
                    "SELECT f.id, f.text, f.tags, f.created FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid "
                    "WHERE facts_fts MATCH ? ORDER BY bm25(facts_fts) LIMIT ?",
                    (match, limit),
                )
            except sqlite3.OperationalError:
                pass
        clause = " OR ".join("lower(text || ' ' || tags) LIKE ?" for _ in words)
        return self._all(
            f"SELECT id, text, tags, created FROM facts WHERE {clause} ORDER BY updated DESC LIMIT ?",
            tuple(f"%{w}%" for w in words) + (limit,),
        )

    # -- chat log --------------------------------------------------------
    def log(self, role: str, text: str) -> None:
        if text.strip():
            self._run("INSERT INTO log(ts, role, text) VALUES (?,?,?)", (time.time(), role, text))

    def recent_log(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._all("SELECT ts, role, text FROM log ORDER BY id DESC LIMIT ?", (limit,))
        return rows[::-1]

    def search_log(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        words = _words(query)
        if not words:
            return []
        clause = " AND ".join("lower(text) LIKE ?" for _ in words)
        return self._all(
            f"SELECT ts, role, text FROM log WHERE {clause} ORDER BY id DESC LIMIT ?",
            tuple(f"%{w}%" for w in words) + (limit,),
        )

    # -- drafts ----------------------------------------------------------
    def save_draft(self, kind: str, target: str, subject: str, body: str, meta: dict | None = None) -> int:
        return self._insert(
            "INSERT INTO drafts(kind, target, subject, body, meta, created) VALUES (?,?,?,?,?,?)",
            (kind, target, subject, body, json.dumps(meta or {}), time.time()),
        )

    def get_draft(self, draft_id: int) -> dict[str, Any] | None:
        rows = self._all("SELECT * FROM drafts WHERE id=?", (draft_id,))
        if rows:
            rows[0]["meta"] = json.loads(rows[0]["meta"])
        return rows[0] if rows else None

    def list_drafts(self, kind: str | None = None) -> list[dict[str, Any]]:
        if kind:
            return self._all("SELECT id, kind, target, subject, status, created FROM drafts WHERE kind=? ORDER BY id DESC", (kind,))
        return self._all("SELECT id, kind, target, subject, status, created FROM drafts ORDER BY id DESC")

    def set_draft_status(self, draft_id: int, status: str) -> None:
        self._run("UPDATE drafts SET status=? WHERE id=?", (status, draft_id))

    def delete_draft(self, draft_id: int) -> bool:
        return self._run("DELETE FROM drafts WHERE id=?", (draft_id,)) > 0

    # -- radar devices ---------------------------------------------------
    def upsert_device(self, dev: dict[str, Any]) -> bool:
        """Store a device seen on the network. Returns True if it is new."""
        now = time.time()
        existing = self._all("SELECT mac FROM devices WHERE mac=?", (dev["mac"],))
        info = json.dumps({k: dev.get(k) for k in ("open_ports", "services", "ssdp", "randomized_mac", "confidence")})
        if existing:
            self._run(
                "UPDATE devices SET ip=?, hostname=COALESCE(NULLIF(?, ''), hostname), vendor=?, kind=?, last_seen=?, info=? WHERE mac=?",
                (dev.get("ip"), dev.get("hostname") or "", dev.get("vendor"), dev.get("kind"), now, info, dev["mac"]),
            )
            return False
        self._run(
            "INSERT INTO devices(mac, ip, hostname, vendor, kind, first_seen, last_seen, info) VALUES (?,?,?,?,?,?,?,?)",
            (dev["mac"], dev.get("ip"), dev.get("hostname") or "", dev.get("vendor"), dev.get("kind"), now, now, info),
        )
        return True

    def device(self, mac: str) -> dict[str, Any] | None:
        rows = self._all("SELECT * FROM devices WHERE mac=?", (mac.lower(),))
        return rows[0] if rows else None

    def devices(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM devices ORDER BY last_seen DESC")

    def label_device(self, mac: str, label: str | None = None, trusted: bool | None = None) -> bool:
        mac = mac.lower()
        if not self.device(mac):
            return False
        if label is not None:
            self._run("UPDATE devices SET label=? WHERE mac=?", (label, mac))
        if trusted is not None:
            self._run("UPDATE devices SET trusted=? WHERE mac=?", (int(trusted), mac))
        return True

    # -- scheduled tasks -------------------------------------------------
    def add_task(self, name: str, prompt: str, schedule: str, next_run: float | None) -> int:
        return self._insert(
            "INSERT INTO tasks(name, prompt, schedule, next_run, created) VALUES (?,?,?,?,?)",
            (name, prompt, schedule, next_run, time.time()),
        )

    def tasks(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM tasks ORDER BY id")

    def due_tasks(self, now: float) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM tasks WHERE enabled=1 AND next_run IS NOT NULL AND next_run<=?", (now,))

    def finish_task(self, task_id: int, result: str, next_run: float | None) -> None:
        self._run(
            "UPDATE tasks SET last_run=?, last_result=?, next_run=?, enabled=CASE WHEN ? IS NULL THEN 0 ELSE enabled END WHERE id=?",
            (time.time(), result[:4000], next_run, next_run, task_id),
        )

    def delete_task(self, task_id: int) -> bool:
        return self._run("DELETE FROM tasks WHERE id=?", (task_id,)) > 0
