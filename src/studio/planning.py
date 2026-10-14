#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Copyright (c) 2026 Sixun Dong
# Author: Sixun Dong
# Licensed under the Apache License, Version 2.0
"""
Live episode planning: run the CineCrew crew (LLM) on a script and import the resulting blueprint.

The crew is a paid operation, so it follows the same rules as media generation: live mode only, credentials
required, one reservation (planning.max_usd_per_episode) held under the project budget before the first LLM
call, settled from the token usage recorded in the trace (or committed at the reservation when prices per token
are unknown). The studio's persistent assets are injected as the AssetLibrary, so the crew references the series'
stable IDs instead of inventing new characters; Art Department runs only for a project without assets.
Media execution is never done here (production_operator.execute stays false): the engine renders.
"""

import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

from . import domain
from .budget import BudgetBlocked
from .providers import load_provider_config


class PlanningRefused(Exception):
    pass


def _planning_price(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return ((config or load_provider_config()).get("live") or {}).get("planning") or {}


def usage_from_logs(log_dir: Path) -> Dict[str, int]:
    """Sum prompt/completion tokens over the trace spans the crew wrote."""
    tot = {"prompt_tokens": 0, "completion_tokens": 0, "spans": 0}
    for f in sorted(Path(log_dir).glob("*_span_*.md")):
        t = f.read_text(encoding="utf-8", errors="ignore")
        tot["spans"] += 1
        for k in ("prompt_tokens", "completion_tokens"):
            m = re.search(r"\*\*" + k + r"\*\*: (\d+)", t)
            tot[k] += int(m.group(1)) if m else 0
    return tot


def planning_cost(usage: Dict[str, int], price: Dict[str, Any]) -> Optional[float]:
    i, o = price.get("input_usd_per_1m_tokens"), price.get("output_usd_per_1m_tokens")
    if i is None or o is None:
        return None
    return round(usage["prompt_tokens"] / 1e6 * float(i) + usage["completion_tokens"] / 1e6 * float(o), 6)


def plan_episode_live(studio, project_id: str, title: str, script: str,
                      provider_config: Optional[Dict[str, Any]] = None, run_pipeline=None) -> Dict[str, Any]:
    store = studio.store
    project = store.get("projects", project_id)
    if project["mode"] != "live":
        raise PlanningRefused("script planning calls the LLM crew; switch the project to live mode first "
                              "(offline projects create episodes from shots or a template)")
    if run_pipeline is None:          # injectable for tests
        if not os.getenv("LLM_API_KEY"):
            raise PlanningRefused("LLM_API_KEY is not set")
        from ..pipeline import run_pipeline
    price = _planning_price(provider_config or studio.engine.provider_config)
    cap = price.get("max_usd_per_episode")
    if cap is None:
        raise BudgetBlocked("planning: providers.yaml live.planning.max_usd_per_episode is null — "
                            "refusing paid LLM calls without an upper bound")
    rid = studio.engine.budget.reserve(project_id, cap, "planning", note=f"crew planning: {title}")
    run_root = studio.home / "planning" / f"{project_id}_{int(time.time())}"
    assets = domain.assets_by_asset_id(store, project_id)
    initial = {}
    if assets:
        initial["asset_library"] = domain.asset_library(store, project_id, title,
                                                        project["settings"].get("global_style", ""))
    try:
        ctx = run_pipeline(script, run_root=run_root, initial_ctx=initial or None,
                           stages=_planning_stages(skip_art=bool(assets)))
    except Exception as e:
        usage = usage_from_logs(run_root / "logs")
        cost = planning_cost(usage, price)
        # calls were made before the failure: charge what the trace shows (or the cap when unknown)
        studio.engine.budget.commit(rid, cost, f"tokens {usage}" if cost is not None else "")
        raise PlanningRefused(f"crew planning failed: {e}") from e
    usage = usage_from_logs(run_root / "logs")
    cost = planning_cost(usage, price)
    studio.engine.budget.commit(rid, cost, f"tokens {usage}" if cost is not None else "")
    bp = ctx["scene_blueprint"]
    if not assets and ctx.get("asset_library") is not None:
        _import_library(store, project_id, ctx["asset_library"])
    return domain.create_episode_from_blueprint(
        store, project_id, title, bp, script=script,
        planning={"mode": "crew", "run_root": str(run_root), "usage": usage, "cost_usd": cost,
                  "coverage": ctx.get("coverage_report")})


def _planning_stages(skip_art: bool):
    from ..pipeline import get_enabled_stages
    out = []
    for s in get_enabled_stages():
        name = s.get("id")
        if skip_art and name == "art_department":
            continue
        if name == "production_operator":
            s = dict(s, execute=False)
        out.append(s)
    return out


def _import_library(store, project_id: str, lib) -> None:
    """Persist a crew-made AssetLibrary as studio assets (same stable IDs)."""
    for kind, items in (("character", lib.characters), ("location", lib.locations), ("prop", lib.props)):
        for a in items or []:
            identity = getattr(a, "description", None) or getattr(a, "visual_style", None) or \
                getattr(a, "visual_description", "")
            data = {"asset_id": a.id, "identity": identity}
            domain.add_asset(store, project_id, kind, getattr(a, "name", a.id), data)
