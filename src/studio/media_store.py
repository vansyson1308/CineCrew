#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Copyright (c) 2026 Sixun Dong
# Author: Sixun Dong
# Licensed under the Apache License, Version 2.0
"""
MediaStore — content-addressed files under <studio_home>/media/<sha[:2]>/<sha>.<ext>.

The only seam the rest of the studio uses to keep or find a file, so an object-storage
implementation (S3 / GCS) can replace it without touching jobs or exports.
"""

import mimetypes
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

from .hashing import hash_file
from .store import Store, now


class MediaStore:
    def __init__(self, root: Path, store: Store):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = store

    def put(self, src: Path, kind: str, meta: Optional[Dict[str, Any]] = None, fixture: bool = False,
            move: bool = False) -> Dict[str, Any]:
        """Copy (or move) a file into the store; returns the media row. Idempotent by content."""
        src = Path(src)
        sha = hash_file(src)
        existing = self.get(sha)
        if existing and Path(existing["path"]).exists():
            if move:
                src.unlink(missing_ok=True)
            return existing
        dest = self.root / sha[:2] / f"{sha}{src.suffix.lower()}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        (shutil.move if move else shutil.copyfile)(str(src), str(dest))
        row = {"sha": sha, "path": str(dest), "kind": kind, "mime": mimetypes.guess_type(dest.name)[0],
               "bytes": dest.stat().st_size, "meta": meta or {}, "fixture": int(fixture), "created_at": now()}
        self.store.conn().execute("DELETE FROM media WHERE sha = ?", (sha,))
        self.store.insert("media", row)
        return row

    def get(self, sha: Optional[str]) -> Optional[Dict[str, Any]]:
        if not sha:
            return None
        return self.store.one("SELECT * FROM media WHERE sha = ?", (sha,))

    def path(self, sha: Optional[str]) -> Optional[Path]:
        row = self.get(sha)
        return Path(row["path"]) if row else None

    def scratch(self, name: str) -> Path:
        """A private working directory (not content-addressed)."""
        p = self.root.parent / "work" / name
        p.mkdir(parents=True, exist_ok=True)
        return p
