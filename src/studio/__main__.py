#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Copyright (c) 2026 Sixun Dong
# Author: Sixun Dong
# Licensed under the Apache License, Version 2.0
"""
CineCrew Studio command line.

  python -m src.studio serve [--port 8765] [--no-worker]   API + director workspace (+ worker thread)
  python -m src.studio worker                              standalone worker process (resumes on start)
  python -m src.studio demo [--out DIR]                    OFFLINE FIXTURE end-to-end: 2 episodes, exports, comic
  python -m src.studio canary --budget USD                 LIVE: one shot of episode 1 on the configured providers
  python -m src.studio status                              projects, budgets, job states
"""

import argparse
import json
import math
import os
import shutil
import sys
import threading
import time
from pathlib import Path

from . import Studio, default_home


def _serve(args):
    import uvicorn
    from .api import create_app
    app = create_app(Studio(args.home), start_worker=not args.no_worker)
    print(f"CineCrew Studio on http://{args.host}:{args.port}  (home: {args.home or default_home()})", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


def _worker(args):
    s = Studio(args.home)
    stop = threading.Event()
    print("worker started; Ctrl-C to stop", flush=True)
    try:
        s.engine.serve_forever(stop)
    except KeyboardInterrupt:
        stop.set()


def _demo(args):
    """Offline fixture run of the acceptance fixture: both episodes, revision, exports, carousel."""
    from . import revisions, domain
    from .comic import export_comic
    home = Path(args.home or (default_home().parent / "studio_demo"))
    if args.fresh and home.exists():
        shutil.rmtree(home)
    s = Studio(home)
    p = s.create_series("hanoi_delivery", mode="offline_fixture")
    eps = s.store.q("SELECT * FROM episodes WHERE project_id = ? ORDER BY number", (p["id"],))
    t0 = time.time()
    s.engine.run_until_idle(p["id"], timeout=600)
    for e in eps:
        s.approve_storyboard(e["id"])
    s.engine.run_until_idle(p["id"], timeout=600)
    report = {"mode": "offline_fixture", "NOTE": "OFFLINE FIXTURE media — proves plumbing, not AI integrations",
              "project_id": p["id"], "home": str(home), "episodes": []}
    for e in eps:
        ex = s.export(e["id"], aspect="9:16")
        ex169 = s.export(e["id"], aspect="16:9")
        comic = export_comic(s, e["id"])
        report["episodes"].append({"title": e["title"], "status": ex["status"], "film_9x16": ex["files"]["film"],
                                   "film_16x9": ex169["files"]["film"], "comic": comic["files"],
                                   "checks_pass": ex["manifest"]["inspection"]["all_pass"],
                                   "duration": ex["manifest"]["inspection"]["probe"]["duration"]})
    # one directed revision on episode 1, then re-export
    e1 = eps[0]
    bp = domain.load_blueprint(s.store.get("episodes", e1["id"]))
    patch = revisions.resolve_edit("Đổi ánh sáng cảnh 3 thành ánh đèn lồng đỏ ấm, trời chạng vạng", bp,
                                   domain.assets_by_asset_id(s.store, p["id"]))
    before = s.store.one("SELECT COUNT(*) AS n FROM attempts WHERE purpose = 'generate'")["n"]
    revisions.apply(s, e1["id"], patch)
    s.engine.run_until_idle(p["id"], timeout=600)
    after = s.store.one("SELECT COUNT(*) AS n FROM attempts WHERE purpose = 'generate'")["n"]
    ex = s.export(e1["id"], aspect="9:16")
    report["revision"] = {"request": patch.source_text, "new_generation_attempts": after - before,
                          "film": ex["files"]["film"], "status": ex["status"]}
    again = s.export(e1["id"], aspect="9:16")
    report["reexport_unchanged_reused"] = again["reused"]
    report["seconds"] = round(time.time() - t0, 1)
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for i, epr in enumerate(report["episodes"], 1):
            for k in ("film_9x16", "film_16x9"):
                shutil.copy(epr[k], out / f"ep{i}_{k}.mp4")
            src = Path(epr["film_9x16"]).parent
            for f in ("subtitles.srt", "subtitles.ass", "contact_sheet.png", "manifest.json", "report.md"):
                shutil.copy(src / f, out / f"ep{i}_{f}")
            for f in Path(next(iter(epr["comic"].values()))).parent.glob("*"):
                shutil.copy(f, out / f"ep{i}_comic_{f.name}")
        shutil.copy(report["revision"]["film"], out / "ep1_revised_shot3_9x16.mp4")
        (out / "demo_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


def _canary(args):
    """
    LIVE canary: a one-shot project on the configured providers, under an explicit budget.
    Refuses to start without credentials and a non-zero budget; prints the ledger at the end.
    """
    from . import domain
    if args.budget <= 0:
        sys.exit("canary needs --budget > 0 (USD) — this run makes paid calls")
    s = Studio(args.home)
    t = domain.load_template("hanoi_delivery")
    t["episodes"] = [dict(t["episodes"][0], shots=t["episodes"][0]["shots"][1:2], title="Canary")]
    p = domain.create_project(s.store, "Canary " + time.strftime("%Y-%m-%d %H:%M"), mode="live",
                              budget_limit_usd=args.budget,
                              settings={"aspect": t["aspect"], "global_style": t["global_style"],
                                        "template_id": "hanoi_delivery"})
    for a in t["assets"]:
        domain.add_asset(s.store, p["id"], a["kind"], a["name"], {k: v for k, v in a.items() if k not in ("kind", "name")})
    ep = domain.create_episode_from_spec(s.store, p["id"], t["episodes"][0])
    for kind in ("image", "video", "tts", "judge"):
        prov = s.engine.provider_for(p, kind)
        print(f"{kind:6s} -> {prov.name} {prov.model} price={prov.price}")
    s.engine.recover()
    s.engine.run_until_idle(p["id"], timeout=args.timeout)
    s.approve_storyboard(ep["id"]) if all(
        j["state"] == "done" for j in domain.jobs_for_episode(s.store, ep["id"]) if j["key"].startswith("kf:")) else None
    s.engine.run_until_idle(p["id"], timeout=args.timeout)
    r = s.engine.readiness(ep["id"])
    print(json.dumps({"readiness": r, "budget": s.engine.budget.summary(p["id"])}, ensure_ascii=False, indent=2))
    if r["status"] in ("final", "draft"):
        ex = s.export(ep["id"], allow_draft=True)
        print("export:", ex["status"], ex["files"]["film"])


def _providers(args):
    """Which live backend serves each kind, through which route, with or without a key and a price."""
    from .providers import NotConfigured, Registry, load_provider_config
    reg = Registry("live", Path(args.home or default_home()) / "provider_state", load_provider_config())
    for kind in ("image", "video", "tts", "judge"):
        try:
            prov = reg.get(kind)
        except NotConfigured as e:
            print(f"{kind:6s} NOT CONFIGURED: {e}")
            continue
        d = prov.describe()
        key = bool(getattr(prov, "api_key", None)) if kind != "judge" else bool(os.getenv("LLM_API_KEY"))
        print(f"{kind:6s} {d['name']:16s} model={d['model']}  route={d.get('route', 'direct')}  "
              f"key={'yes' if key else 'MISSING'}  price={'set' if prov.estimate_cost({'duration': 1, 'text': 'x'}) is not None else 'MISSING (blocks paid calls)'}")


def _estimate(args):
    """
    Cost range of a film of --minutes on the configured live prices, before any call is made.
    low = every job accepted on its first attempt; high = every job uses all its attempts (max_attempts).
    With --budget, the longest film that fits under the budget in the worst case.
    """
    from .providers import NotConfigured, Registry, load_provider_config
    reg = Registry("live", Path(args.home or default_home()) / "provider_state", load_provider_config())
    shots = math.ceil(args.minutes * 60 / args.shot_seconds)
    chars = args.minutes * 60 * args.speech_chars_per_second
    unit = {}
    try:
        unit["image"] = reg.get("image").estimate_cost({})
        unit["video"] = reg.get("video").estimate_cost({"duration": args.shot_seconds})
        unit["tts"] = reg.get("tts").estimate_cost({"text": "x" * int(chars / max(shots, 1))})
        unit["judge"] = reg.get("judge").estimate_cost({})
    except NotConfigured as e:
        sys.exit(f"provider not configured: {e}")
    missing = [k for k, v in unit.items() if v is None]
    if {"image", "video"} & set(missing):
        sys.exit(f"price missing for {missing} in the providers file: cannot estimate (and paid calls stay blocked)")
    unit = {k: (v or 0.0) for k, v in unit.items()}
    per_try = shots * (unit["image"] + unit["video"] + unit["tts"] + 2 * unit["judge"])
    refs = args.assets * unit["image"]
    low, high = per_try + refs, per_try * args.max_attempts + refs * args.max_attempts
    out = {"minutes": args.minutes, "shots": shots, "unit_usd": unit, "low_usd": round(low, 2), "high_usd": round(high, 2),
           "basis": "configured prices; planning LLM not included",
           "not_priced_excluded": missing}
    if args.budget:
        per_min_high = (high - refs * args.max_attempts) / args.minutes
        out["max_minutes_worst_case"] = round(max(0.0, (args.budget - refs * args.max_attempts) / per_min_high), 1)
    print(json.dumps(out, indent=2))


def _status(args):
    s = Studio(args.home)
    for p in s.store.q("SELECT * FROM projects ORDER BY created_at"):
        print(f"{p['id']}  {p['name']}  mode={p['mode']}  budget={s.engine.budget.summary(p['id'])}")
        for e in s.store.q("SELECT * FROM episodes WHERE project_id = ? ORDER BY number", (p["id"],)):
            r = s.engine.readiness(e["id"])
            print(f"   ep{e['number']} {e['title']}: {r['status']} — {r['reason']}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m src.studio")
    ap.add_argument("--home", default=None, help="studio data directory (default $STUDIO_HOME or data/studio)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--no-worker", action="store_true")
    sub.add_parser("worker")
    dp = sub.add_parser("demo")
    dp.add_argument("--out", default=None)
    dp.add_argument("--fresh", action="store_true")
    cp = sub.add_parser("canary")
    cp.add_argument("--budget", type=float, required=True)
    cp.add_argument("--timeout", type=float, default=3600)
    sub.add_parser("status")
    sub.add_parser("providers")
    ep = sub.add_parser("estimate")
    ep.add_argument("--minutes", type=float, required=True)
    ep.add_argument("--shot-seconds", type=float, default=8.0)
    ep.add_argument("--speech-chars-per-second", type=float, default=12.0)
    ep.add_argument("--assets", type=int, default=6, help="reference sheets to render once per series")
    ep.add_argument("--max-attempts", type=int, default=2)
    ep.add_argument("--budget", type=float, default=0.0)
    args = ap.parse_args(argv)
    {"serve": _serve, "worker": _worker, "demo": _demo, "canary": _canary, "status": _status,
     "providers": _providers, "estimate": _estimate}[args.cmd](args)


if __name__ == "__main__":
    main()
