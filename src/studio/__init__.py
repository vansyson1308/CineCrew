#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Copyright (c) 2026 Sixun Dong
# Author: Sixun Dong
# Licensed under the Apache License, Version 2.0
"""
CineCrew Studio — local-first production layer on top of the FilmDSL crew.

    from src.studio import Studio
    studio = Studio("data/studio")                       # SQLite + content-addressed media
    project = studio.create_series("hanoi_delivery")     # offline_fixture mode by default
    studio.engine.run_until_idle(project["id"])          # storyboard (keyframes + voices)
    studio.approve_storyboard(episode_id)
    studio.engine.run_until_idle(project["id"])          # clips
    studio.export(episode_id, aspect="9:16")             # final MP4 + SRT/ASS + manifest (or a labelled draft)

See docs/studio/ for the architecture, audit and usage.
"""

import os
from pathlib import Path
from typing import Any, Dict, Optional

from . import domain
from .engine import Engine
from .media_store import MediaStore
from .store import Store

_ROOT = Path(__file__).resolve().parents[2]


def default_home() -> Path:
    return Path(os.getenv("STUDIO_HOME") or (_ROOT / "data" / "studio"))


class Studio:
    def __init__(self, home: Optional[Path] = None, provider_config: Optional[Dict[str, Any]] = None):
        self.home = Path(home or default_home())
        self.home.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.home / "studio.db")
        self.media = MediaStore(self.home / "media", self.store)
        self.engine = Engine(self.store, self.media, self.home, provider_config)

    # ── creation ───────────────────────────────────────────────────────────
    def create_series(self, template_id: str = "hanoi_delivery", mode: str = "offline_fixture",
                      budget_limit_usd: float = 0.0, name: Optional[str] = None, episodes=None) -> Dict[str, Any]:
        return domain.create_series_from_template(self.store, template_id, mode, budget_limit_usd, name, episodes)

    def approve_storyboard(self, episode_id: str) -> Dict[str, Any]:
        jobs = [j for j in domain.jobs_for_episode(self.store, episode_id) if j["key"].startswith("kf:")]
        not_ready = [j["key"] for j in jobs if not (j["state"] == "done" and j["verdict"] in ("accepted", "unreviewed"))]
        if not_ready:
            raise ValueError(f"storyboard incomplete: {', '.join(not_ready)} not accepted yet")
        self.store.update("episodes", "id", episode_id, {"storyboard_approved": 1})
        n = self.store.conn().execute("UPDATE jobs SET state = 'pending', error = NULL WHERE episode_id = ? AND "
                                      "state = 'blocked' AND error LIKE 'storyboard%'", (episode_id,)).rowcount
        self.store.event("info", f"storyboard approved ({n} clip jobs released)", episode_id=episode_id)
        return self.store.get("episodes", episode_id)

    def set_budget(self, project_id: str, limit_usd: float) -> Dict[str, Any]:
        self.store.update("projects", "id", project_id, {"budget_limit_usd": float(limit_usd)})
        self.engine.unblock(project_id)
        return self.store.get("projects", project_id)

    def export(self, episode_id: str, **kw) -> Dict[str, Any]:
        from .export import export_episode
        return export_episode(self, episode_id, **kw)


__all__ = ["Studio", "default_home"]
