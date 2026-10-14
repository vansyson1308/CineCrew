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
    args = ap.parse_args(argv)
    {"serve": _serve, "worker": _worker, "demo": _demo, "canary": _canary, "status": _status}[args.cmd](args)


if __name__ == "__main__":
    main()
