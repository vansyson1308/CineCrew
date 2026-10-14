#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Copyright (c) 2026 Sixun Dong
# Author: Sixun Dong
# Licensed under the Apache License, Version 2.0
"""
OFFLINE FIXTURE providers — deterministic stand-ins for integration tests and the offline sample mode.

Every image/frame they produce is stamped "OFFLINE FIXTURE" and every media row is flagged
fixture=1. Their output proves the studio's plumbing (states, retries, resume, budget, exports),
never that an AI model integration works.

The video fixture behaves like a remote asynchronous service whose state lives on disk, so a test
can kill the worker mid-render and verify that a restart reconciles instead of resubmitting.

Fault injection (in the prompt / text, so tests stay declarative):
  [[fail]]        generation fails            [[timeout]]     never completes
  [[ratelimit]]   first submit is rate-limited [[reject]]      the fixture judge rejects
  [[judge_error]] the fixture judge raises    [[ambiguous]]   submit raises AmbiguousSubmission once
  [[always_reject]] the judge rejects every attempt ([[reject]] is removed by its revised prompt)
"""

import hashlib
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import avtools
from .base import (
    AmbiguousSubmission, Output, PollResult, Provider, ProviderCapabilities, ProviderError, RateLimited, SubmitResult,
)

_LOCK = threading.Lock()


def _color(seed: str) -> str:
    h = hashlib.sha256(seed.encode()).hexdigest()
    r, g, b = (int(h[i:i + 2], 16) // 2 + 40 for i in (0, 2, 4))
    return f"0x{r:02x}{g:02x}{b:02x}"


def _label(workdir: Path, name: str, lines: List[str], width_chars: int = 40) -> Path:
    import textwrap
    body = [w for l in lines if l for w in textwrap.wrap(l, width_chars)[:3]]
    return avtools.write_text_file(workdir / f"{name}.txt", "OFFLINE FIXTURE\n" + "\n".join(body))


def _wh(size: str):
    w, h = size.lower().split("x")
    return int(w), int(h)


class _State:
    """Remote-service state on disk: <state_dir>/<remote_id>.json + counters."""

    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, rid: str) -> Path:
        return self.dir / f"{rid}.json"

    def load(self, rid: str) -> Optional[Dict[str, Any]]:
        p = self.path(rid)
        return json.loads(p.read_text()) if p.exists() else None

    def save(self, rid: str, data: Dict[str, Any]) -> None:
        tmp = self.path(rid).with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(self.path(rid))

    def bump(self, counter: str) -> int:
        with _LOCK:
            p = self.dir / "_counters.json"
            c = json.loads(p.read_text()) if p.exists() else {}
            c[counter] = c.get(counter, 0) + 1
            p.write_text(json.dumps(c))
            return c[counter]

    def counters(self) -> Dict[str, int]:
        p = self.dir / "_counters.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def find_key(self, key: str) -> Optional[str]:
        for p in self.dir.glob("*.json"):
            if p.name.startswith("_"):
                continue
            d = json.loads(p.read_text())
            if d.get("idempotency_key") == key:
                return d["id"]
        return None


class FixtureImageProvider(Provider):
    name, kind, model, is_fixture = "fixture-image", "image", "fixture-image-v1", True
    caps = ProviderCapabilities(
        kind="image", accepted_fields=("prompt", "negative_prompt", "size", "seed", "reference_images", "label"),
        max_reference_images=4, negative_prompt=True, seed=True, idempotency=True, usage_reporting=True,
        constraints=["OFFLINE FIXTURE: flat colour card with the prompt printed on it"],
    )

    def submit(self, request, idempotency_key, workdir):
        if "[[fail]]" in request.get("prompt", ""):
            raise ProviderError("fixture image: injected failure")
        workdir.mkdir(parents=True, exist_ok=True)
        w, h = _wh(request.get("size", "576x1024"))
        rid = f"img_{uuid.uuid4().hex[:10]}"
        out = workdir / f"{rid}.png"
        label = _label(workdir, rid, [request.get("label", ""), request.get("prompt", "")[:120],
                                      f"refs: {len(request.get('reference_images') or [])} seed: {request.get('seed')}"],
                       width_chars=max(16, w // max(8, w // 26) * 10 // 6))
        fs = max(10, w // 26)
        avtools.run(["-f", "lavfi", "-i", f"color=c={_color(request.get('prompt', ''))}:s={w}x{h}:d=1",
                     "-vf", f"drawtext=fontfile={avtools.FONT}:textfile={avtools.esc_path(label)}:fontcolor=white:"
                            f"fontsize={fs}:x=(w-text_w)/2:y=(h-text_h)/2:line_spacing=6:box=1:boxcolor=black@0.45",
                     "-frames:v", "1", str(out)])
        return SubmitResult(rid, Output(out, "image/png", usage={"images": 1}, meta={"width": w, "height": h}))


class FixtureVideoProvider(Provider):
    name, kind, model, is_fixture = "fixture-video", "video", "fixture-video-v1", True
    caps = ProviderCapabilities(
        kind="video",
        accepted_fields=("prompt", "negative_prompt", "duration", "size", "fps", "seed", "image", "reference_images",
                         "label", "audio"),
        durations=[2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0], max_reference_images=3, first_frame_image=True,
        generates_audio=True, lip_sync=False, negative_prompt=True, seed=True, idempotency=True, cancellation=True,
        usage_reporting=True, async_polling=True,
        constraints=["OFFLINE FIXTURE: animated test pattern + the shot's prompt; 'audio' is a quiet tone"],
    )

    def __init__(self, state_dir: Path, latency: float = 0.3, price: Optional[Dict[str, Any]] = None):
        super().__init__(price)
        self.state = _State(state_dir)
        self.latency = latency

    def submit(self, request, idempotency_key, workdir):
        prompt = request.get("prompt", "")
        existing = self.state.find_key(idempotency_key) if idempotency_key else None
        if existing:
            self.state.bump("idempotent_replays")
            return SubmitResult(existing)
        # a rate limit is about the service, not the request id: the first try of this prompt gets a 429
        if "[[ratelimit]]" in prompt and self.state.bump(f"rl_{hashlib.md5(prompt.encode()).hexdigest()}") == 1:
            raise RateLimited("fixture video: injected 429", retry_after=0.05)
        if "[[ambiguous]]" in prompt and self.state.bump(f"amb_{idempotency_key}") == 1:
            # The service DID create the job, but the client never learned its id.
            rid = f"vid_{uuid.uuid4().hex[:10]}"
            self._create(rid, request, idempotency_key)
            raise AmbiguousSubmission("fixture video: connection dropped after send")
        rid = f"vid_{uuid.uuid4().hex[:10]}"
        self._create(rid, request, idempotency_key)
        return SubmitResult(rid)

    def _create(self, rid, request, key):
        self.state.bump("submissions")
        self.state.save(rid, {"id": rid, "request": request, "idempotency_key": key, "created": time.time(),
                              "state": "queued", "cancelled": False})

    def poll(self, remote_id):
        d = self.state.load(remote_id)
        if d is None:
            return PollResult("unknown", error="no such job on the fixture service")
        if d.get("cancelled"):
            return PollResult("failed", error="cancelled")
        prompt = d["request"].get("prompt", "")
        if "[[timeout]]" in prompt:
            return PollResult("running", progress=0.5)
        if time.time() - d["created"] < self.latency:
            return PollResult("running", progress=(time.time() - d["created"]) / self.latency)
        if "[[fail]]" in prompt:
            return PollResult("failed", error="fixture video: injected failure")
        return PollResult("completed", progress=1.0)

    def fetch(self, remote_id, workdir):
        d = self.state.load(remote_id)
        req = d["request"]
        workdir.mkdir(parents=True, exist_ok=True)
        w, h = _wh(req.get("size", "576x1024"))
        dur = float(req.get("duration", 4))
        fps = int(req.get("fps", 24) or 24)
        out = workdir / f"{remote_id}.mp4"
        label = _label(workdir, remote_id, [req.get("label", ""), req.get("prompt", "")[:120],
                                            f"{dur:g}s {w}x{h} first-frame:{'yes' if req.get('image') else 'no'}"],
                       width_chars=max(16, w // max(8, w // 28) * 10 // 6))
        fs = max(10, w // 28)
        args = ["-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:r={fps}:d={dur}"]
        if req.get("image"):
            args = ["-loop", "1", "-t", f"{dur}", "-i", req["image"]]
        args += ["-f", "lavfi", "-i", f"sine=frequency=220:sample_rate=48000:duration={dur}"]
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
              f"fps={fps},drawtext=fontfile={avtools.FONT}:textfile={avtools.esc_path(label)}:fontcolor=white:"
              f"fontsize={fs}:x=(w-text_w)/2:y=h*0.62+10*sin(t*3):box=1:boxcolor=black@0.5,"
              f"drawbox=x=mod(t*{w // 3}\\,{w}):y=0:w=12:h=ih:color=yellow@0.6:t=fill")
        avtools.run(args + ["-vf", vf, "-af", "volume=0.05", "-t", f"{dur}", "-c:v", "libx264", "-preset", "ultrafast",
                            "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out)])
        return Output(out, "video/mp4", usage={"seconds": dur}, meta={"width": w, "height": h, "fps": fps})

    def cancel(self, remote_id):
        d = self.state.load(remote_id)
        if d is None:
            return False
        d["cancelled"] = True
        self.state.save(remote_id, d)
        return True

    def stats(self) -> Dict[str, int]:
        return self.state.counters()


class FixtureTTSProvider(Provider):
    """Tone whose length follows the syllable count (~4.5 syllables/s): exercises timing, not speech."""

    name, kind, model, is_fixture = "fixture-tts", "tts", "fixture-tts-v1", True
    caps = ProviderCapabilities(
        kind="tts", accepted_fields=("text", "voice", "instructions", "speed", "format", "language"),
        max_text_chars=4096, voices=["fixture_a", "fixture_b", "fixture_narrator"], idempotency=True,
        usage_reporting=True, constraints=["OFFLINE FIXTURE: a tone, not speech"],
    )

    def submit(self, request, idempotency_key, workdir):
        text = request.get("text", "")
        if "[[fail]]" in text:
            raise ProviderError("fixture tts: injected failure")
        workdir.mkdir(parents=True, exist_ok=True)
        syllables = max(1, len(text.split()))
        dur = round(syllables / 4.5 / float(request.get("speed", 1.0) or 1.0) + 0.25, 3)
        freq = 180 + (int(hashlib.md5(str(request.get("voice")).encode()).hexdigest(), 16) % 200)
        rid = f"tts_{uuid.uuid4().hex[:10]}"
        out = workdir / f"{rid}.wav"
        avtools.run(["-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000:duration={dur}",
                     "-af", "volume=0.3,afade=t=in:d=0.05,afade=t=out:st=" + f"{max(0, dur - 0.08):.3f}:d=0.08",
                     "-ac", "1", str(out)])
        return SubmitResult(rid, Output(out, "audio/wav", usage={"characters": len(text)}, meta={"fixture_tone": True}))


class FixtureJudgeProvider(Provider):
    """Deterministic reviewer. Records what it was shown so tests can assert references were attached."""

    name, kind, model, is_fixture = "fixture-judge", "judge", "fixture-judge-v1", True
    caps = ProviderCapabilities(
        kind="judge", accepted_fields=("media", "references", "prompt", "shot_context", "media_kind", "global_style"),
        max_reference_images=16, usage_reporting=True,
        constraints=["OFFLINE FIXTURE: accepts unless the prompt carries [[reject]]"],
    )

    def __init__(self, price=None):
        super().__init__(price)
        self.calls: List[Dict[str, Any]] = []

    def submit(self, request, idempotency_key, workdir):
        self.calls.append(request)
        prompt = request.get("prompt", "")
        if "[[judge_error]]" in prompt:
            raise ProviderError("fixture judge: injected failure", retryable=False)
        rejected = "[[reject]]" in prompt or "[[always_reject]]" in prompt
        review = {
            "accepted": not rejected, "score": 0.35 if rejected else 0.9,
            "issues": ["fixture: injected rejection"] if rejected else [],
            "revised_prompt": prompt.replace("[[reject]]", "").strip() if rejected else None,
            "notes": f"fixture judge saw {len(request.get('references') or [])} reference image(s)",
            "references_seen": [r.get("label") for r in request.get("references") or []],
        }
        return SubmitResult(f"jdg_{uuid.uuid4().hex[:8]}", Output(Path("/dev/null"), "application/json",
                                                                usage={"calls": 1}, meta={"review": review}))
