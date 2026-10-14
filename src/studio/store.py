#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Copyright (c) 2026 Sixun Dong
# Author: Sixun Dong
# Licensed under the Apache License, Version 2.0
"""
Durable studio state (SQLite, WAL). All SQL lives here, so moving to PostgreSQL is a driver swap.

Tables
  projects        series / brand / explainer project, mode (offline_fixture | live), settings, budget limit
  assets          character / location / prop / product / logo / voice / music; identity JSON, lock, canonical media
  episodes        one FilmDSL SceneBlueprint per episode (+ studio sidecar per shot), current version
  episode_versions immutable snapshots of the blueprint after every applied revision (rollback = restore one)
  jobs            execution graph nodes: keyframe / clip / tts / assemble / export ... with three state axes
  attempts        immutable provider-call records (request is never updated after insert)
  ledger          budget reservations / commits / releases
  media           content-addressed files (sha256)
  exports         produced films / subtitles / carousels with manifest + readiness label
  events          append-only log for the UI (job progress, failures)
"""

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'series',
  mode TEXT NOT NULL DEFAULT 'offline_fixture', settings TEXT NOT NULL DEFAULT '{}',
  budget_limit_usd REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS assets (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
  data TEXT NOT NULL DEFAULT '{}', locked INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1,
  content_hash TEXT, canonical_media TEXT, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS episodes (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, number INTEGER NOT NULL, title TEXT NOT NULL,
  script TEXT NOT NULL DEFAULT '', blueprint TEXT NOT NULL, studio TEXT NOT NULL DEFAULT '{}',
  version INTEGER NOT NULL DEFAULT 1, storyboard_approved INTEGER NOT NULL DEFAULT 0,
  planning TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS episode_versions (
  episode_id TEXT NOT NULL, version INTEGER NOT NULL, blueprint TEXT NOT NULL, studio TEXT NOT NULL,
  patch TEXT, note TEXT, created_at REAL NOT NULL, PRIMARY KEY (episode_id, version)
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, episode_id TEXT NOT NULL, key TEXT NOT NULL, kind TEXT NOT NULL,
  shot_id TEXT, deps TEXT NOT NULL DEFAULT '[]', spec TEXT NOT NULL DEFAULT '{}', spec_hash TEXT,
  input_hash TEXT, mandatory INTEGER NOT NULL DEFAULT 1,
  state TEXT NOT NULL DEFAULT 'pending',          -- pending|ready|running|waiting_review|done|needs_review|failed|blocked|cancelled|stale
  provider_state TEXT NOT NULL DEFAULT 'none',    -- none|reserved|submitting|submitted|running|completed|failed|timed_out|cancelled|unknown
  verdict TEXT NOT NULL DEFAULT 'pending',        -- pending|accepted|rejected|review_error|unreviewed
  verdict_source TEXT, selected_attempt TEXT, locked INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 2, transport_retries INTEGER NOT NULL DEFAULT 0,
  next_run_at REAL NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0,
  lease_until REAL NOT NULL DEFAULT 0, error TEXT, cache_hit INTEGER NOT NULL DEFAULT 0,
  force_new INTEGER NOT NULL DEFAULT 0, epoch REAL NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE (episode_id, key)
);
CREATE TABLE IF NOT EXISTS attempts (
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, n INTEGER NOT NULL, purpose TEXT NOT NULL DEFAULT 'generate',
  provider TEXT NOT NULL, model TEXT, request TEXT NOT NULL, request_hash TEXT NOT NULL, input_hash TEXT,
  reference_hashes TEXT NOT NULL DEFAULT '{}', seed INTEGER, idempotency_key TEXT,
  remote_id TEXT, provider_state TEXT NOT NULL DEFAULT 'created',
  submitted_at REAL, completed_at REAL, last_polled_at REAL,
  verdict TEXT NOT NULL DEFAULT 'pending', review TEXT, error TEXT,
  usage TEXT, cost_estimate_usd REAL, cost_actual_usd REAL, reservation_id TEXT,
  media_sha TEXT, media_meta TEXT, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS attempts_input ON attempts (input_hash, verdict);
CREATE TABLE IF NOT EXISTS ledger (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, job_id TEXT, attempt_id TEXT, kind TEXT NOT NULL,
  state TEXT NOT NULL,                 -- reserved|committed|released
  amount_usd REAL NOT NULL, estimate_usd REAL NOT NULL, cost_basis TEXT, note TEXT,
  created_at REAL NOT NULL, settled_at REAL
);
CREATE TABLE IF NOT EXISTS media (
  sha TEXT PRIMARY KEY, path TEXT NOT NULL, kind TEXT NOT NULL, mime TEXT, bytes INTEGER,
  meta TEXT NOT NULL DEFAULT '{}', fixture INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS exports (
  id TEXT PRIMARY KEY, episode_id TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
  input_hash TEXT, files TEXT NOT NULL DEFAULT '{}', manifest TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, episode_id TEXT, job_id TEXT,
  level TEXT NOT NULL, message TEXT NOT NULL, created_at REAL NOT NULL
);
"""

_JSON_COLS = {
    "settings", "data", "blueprint", "studio", "planning", "patch", "deps", "spec", "request",
    "reference_hashes", "review", "usage", "media_meta", "meta", "files", "manifest",
}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _row(r: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if r is None:
        return None
    d = dict(r)
    for k, v in d.items():
        if k in _JSON_COLS and isinstance(v, str):
            try:
                d[k] = json.loads(v)
            except ValueError:
                pass
    return d


def _enc(v: Any) -> Any:
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    if isinstance(v, bool):
        return int(v)
    return v


class Store:
    """Thread-safe facade: one connection per thread, short transactions, WAL mode."""

    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        c = self.conn()
        c.executescript(_SCHEMA)  # executescript manages its own transaction
        c.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    # ── connections / transactions ─────────────────────────────────────────
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(str(self.path), timeout=30, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=30000")
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
        return c

    @contextmanager
    def tx(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """A transaction. `immediate=True` takes the write lock up front (budget reservations)."""
        c = self.conn()
        c.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    def q(self, sql: str, args: tuple = ()) -> List[Dict[str, Any]]:
        return [_row(r) for r in self.conn().execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> Optional[Dict[str, Any]]:
        return _row(self.conn().execute(sql, args).fetchone())

    # ── generic helpers ────────────────────────────────────────────────────
    def insert(self, table: str, row: Dict[str, Any], c: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
        cols = list(row)
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
        (c or self.conn()).execute(sql, tuple(_enc(row[k]) for k in cols))
        return row

    def update(self, table: str, key: str, value: Any, fields: Dict[str, Any],
               c: Optional[sqlite3.Connection] = None, where_extra: str = "", extra_args: tuple = ()) -> int:
        if not fields:
            return 0
        sets = ", ".join(f"{k} = ?" for k in fields)
        sql = f"UPDATE {table} SET {sets} WHERE {key} = ?{where_extra}"
        cur = (c or self.conn()).execute(sql, tuple(_enc(v) for v in fields.values()) + (value,) + extra_args)
        return cur.rowcount

    def get(self, table: str, id_: str) -> Optional[Dict[str, Any]]:
        return self.one(f"SELECT * FROM {table} WHERE id = ?", (id_,))

    # ── events ─────────────────────────────────────────────────────────────
    def event(self, level: str, message: str, project_id: str = None, episode_id: str = None, job_id: str = None):
        self.conn().execute(
            "INSERT INTO events (project_id, episode_id, job_id, level, message, created_at) VALUES (?,?,?,?,?,?)",
            (project_id, episode_id, job_id, level, message, time.time()),
        )

    # ── attempts: immutable request ─────────────────────────────────────────
    _ATTEMPT_OUTCOME_FIELDS = {
        "remote_id", "provider_state", "submitted_at", "completed_at", "last_polled_at", "verdict", "review",
        "error", "usage", "cost_actual_usd", "reservation_id", "media_sha", "media_meta", "cost_estimate_usd",
    }

    def update_attempt(self, attempt_id: str, **fields) -> None:
        """Only outcome fields may change; the request, its hash, seed and references never do."""
        bad = set(fields) - self._ATTEMPT_OUTCOME_FIELDS
        if bad:
            raise ValueError(f"attempt fields are immutable: {sorted(bad)}")
        self.update("attempts", "id", attempt_id, fields)


def now() -> float:
    return time.time()
