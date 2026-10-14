# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Licensed under the Apache License, Version 2.0
"""Small offline studio projects for the engineering-gate tests (fixture providers only)."""

import copy
from typing import Callable, Optional

from src.studio import Studio, domain
from src.studio.providers import load_provider_config


def fixture_config(**prices):
    """providers.yaml with optional pretend fixture prices, e.g. image=0.1, video=0.05 (per second)."""
    cfg = copy.deepcopy(load_provider_config())
    for kind, usd in prices.items():
        cfg["offline_fixture"][kind]["price"]["usd"] = usd
    cfg["offline_fixture"]["video"]["latency_seconds"] = 0.2
    return cfg


def small_project(studio: Studio, n_shots: int = 2, mutate: Optional[Callable] = None, budget: float = 0.0,
                  mode: str = "offline_fixture"):
    """Template series with one short episode (first n shots of episode 1). Returns (project, episode)."""
    t = domain.load_template("hanoi_delivery")
    p = domain.create_project(studio.store, "Test series", mode=mode, budget_limit_usd=budget,
                              settings={"aspect": t["aspect"], "global_style": t["global_style"],
                                        "template_id": "hanoi_delivery"})
    for a in t["assets"]:
        domain.add_asset(studio.store, p["id"], a["kind"], a["name"],
                         {k: v for k, v in a.items() if k not in ("kind", "name")})
    spec = copy.deepcopy(t["episodes"][0])
    spec["shots"] = spec["shots"][:n_shots]
    for s in spec["shots"]:
        s["duration"] = min(s["duration"], 4)
    if mutate:
        mutate(spec)
    ep = domain.create_episode_from_spec(studio.store, p["id"], spec)
    return p, ep


def render_all(studio: Studio, p, ep, timeout: float = 180):
    studio.engine.run_until_idle(p["id"], timeout=timeout)
    studio.approve_storyboard(ep["id"])
    studio.engine.run_until_idle(p["id"], timeout=timeout)
    return studio.engine.readiness(ep["id"])


def generate_attempts(studio: Studio) -> int:
    return studio.store.one("SELECT COUNT(*) AS n FROM attempts WHERE purpose = 'generate'")["n"]
