#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Copyright (c) 2026 Sixun Dong
# Author: Sixun Dong
# Licensed under the Apache License, Version 2.0
"""
MotionStillVideoProvider — the "video" stage without a video model.

It turns the shot's approved keyframe (rendered by the image provider from the asset sheets) into a clip
with a deterministic camera move (push-in, pull-out, pan, tilt, or a slow "breathing" push for static
shots), locally with ffmpeg. No network, no model, no cost. This is what makes a film possible with only
an OpenAI key now that OpenAI's video API is gone: OpenAI draws the frames and speaks the lines, the
studio does the camera work, the edit, the subtitles and the mix.

Consistency is inherited from the keyframe: a camera move cannot change a face, so there is nothing
for a clip review to catch that the keyframe review has not already judged (see `review_clips`).

The camera move comes from the shot's FilmDSL `staging_layer.camera.movement` (the engine passes it as
`camera_movement` only to providers that accept it).
"""

from pathlib import Path
from typing import Any, Dict, Tuple

from .. import avtools
from .base import Output, PollResult, Provider, ProviderCapabilities, ProviderError, SubmitResult, UnsupportedParameter

# camera vocabulary (FilmDSL movement strings, lower-cased, '-'/' ' -> '_') -> move
MOVES = {
    "push_in": "push_in", "dolly_in": "push_in", "zoom_in": "push_in", "slow_push_in": "push_in", "track_in": "push_in",
    "pull_out": "pull_out", "dolly_out": "pull_out", "zoom_out": "pull_out", "pull_back": "pull_out",
    "track_out": "pull_out",
    "pan_left": "pan_left", "truck_left": "pan_left", "track_left": "pan_left",
    "pan_right": "pan_right", "truck_right": "pan_right", "track_right": "pan_right", "tracking": "pan_right",
    "tilt_up": "tilt_up", "pedestal_up": "tilt_up", "crane_up": "tilt_up",
    "tilt_down": "tilt_down", "pedestal_down": "tilt_down", "crane_down": "tilt_down",
    "static": "breathe", "locked_off": "breathe", "handheld": "breathe", "none": "breathe",
}


def resolve_move(movement: str, seed: int = 0) -> str:
    m = (movement or "").strip().lower().replace("-", "_").replace(" ", "_")
    if m in MOVES:
        return MOVES[m]
    for k, v in MOVES.items():        # "slow dolly in toward Minh" -> push_in
        if k in m:
            return v
    return "breathe"


class MotionStillVideoProvider(Provider):
    name, kind = "motion-still", "video"
    review_clips = False                # the clip is a camera move over the reviewed keyframe: nothing new to judge
    caps = ProviderCapabilities(
        kind="video", accepted_fields=("prompt", "duration", "size", "image", "seed", "camera_movement", "label"),
        min_duration=0.5, max_duration=600.0, first_frame_image=True, generates_audio=False, lip_sync=False,
        seed=True, idempotency=True, cancellation=False, usage_reporting=True, async_polling=False,
        constraints=["local ffmpeg, no model: camera move over the approved keyframe (no character motion)",
                     "needs the keyframe (first frame); refuses a request without one", "$0"],
        docs="src/studio/providers/motion.py",
    )

    def __init__(self, model: str = "kenburns-v1", cfg: Dict[str, Any] = None, price=None):
        super().__init__(price if price is not None else {"unit": "second", "usd": 0.0})
        cfg = cfg or {}
        self.model = model or "kenburns-v1"
        self.fps = int(cfg.get("fps", 24))
        self.strength = float(cfg.get("motion_strength", 0.12))   # zoom / travel as a fraction of the frame
        self.api_key = "local"                                    # `providers` CLI: no key needed

    # ── geometry ──────────────────────────────────────────────────────────
    def _filter(self, move: str, w: int, h: int, dur: float) -> str:
        """Cover-scale the still to (1+s)x the output, then crop a moving/zooming window. Per-frame scale gives
        sub-zoompan smoothness (zoompan rounds the window position to whole input pixels and jitters)."""
        s = self.strength
        D = max(dur, 0.04)
        p = f"(t/{D:.4f})"
        ease = f"(0.5-0.5*cos(PI*min(1,{p})))"             # ease-in-out: no jolt at the cut
        if move in ("push_in", "pull_out", "breathe"):
            amt = s if move != "breathe" else s * 0.4
            z = f"(1+{amt:.4f}*{ease})" if move != "pull_out" else f"(1+{amt:.4f}*(1-{ease}))"
            return (f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
                    f"scale=w='trunc({w}*{z}/2)*2':h='trunc({h}*{z}/2)*2':eval=frame,"
                    f"crop={w}:{h}:x='(in_w-{w})/2':y='(in_h-{h})/2'")
        big_w, big_h = int(round(w * (1 + s) / 2)) * 2, int(round(h * (1 + s) / 2)) * 2
        if move == "pan_left":
            x, y = f"(in_w-{w})*(1-{ease})", f"(in_h-{h})/2"
        elif move == "pan_right":
            x, y = f"(in_w-{w})*{ease}", f"(in_h-{h})/2"
        elif move == "tilt_up":
            x, y = f"(in_w-{w})/2", f"(in_h-{h})*(1-{ease})"
        else:  # tilt_down
            x, y = f"(in_w-{w})/2", f"(in_h-{h})*{ease}"
        return (f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase,crop={big_w}:{big_h},"
                f"crop={w}:{h}:x='{x}':y='{y}'")

    # ── lifecycle (synchronous) ───────────────────────────────────────────
    def submit(self, request, idempotency_key, workdir):
        self.validate(request)
        img = request.get("image")
        if not img or not Path(img).exists():
            raise UnsupportedParameter("motion-still needs the approved keyframe (first frame) of the shot")
        w, h = (int(v) for v in str(request.get("size", "720x1280")).lower().split("x"))
        w, h = w - w % 2, h - h % 2
        dur = float(request.get("duration", 4) or 4)
        move = resolve_move(request.get("camera_movement", ""), int(request.get("seed") or 0))
        workdir.mkdir(parents=True, exist_ok=True)
        out = workdir / f"motion_{idempotency_key[:12]}.mp4"
        try:
            avtools.run(["-loop", "1", "-framerate", str(self.fps), "-t", f"{dur:.3f}", "-i", str(img),
                         "-vf", self._filter(move, w, h, dur) + ",format=yuv420p,setsar=1",
                         "-r", str(self.fps), "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                         "-movflags", "+faststart", "-an", str(out)])
        except avtools.AVError as e:
            raise ProviderError(f"motion-still: {e}") from e
        info = avtools.probe(out)
        return SubmitResult(f"local_{idempotency_key[:12]}",
                            Output(out, "video/mp4", usage={"seconds": info["duration"]},
                                   meta={**info, "camera_move": move, "source": "keyframe + local camera move"}))

    def poll(self, remote_id) -> PollResult:          # synchronous: submit() already delivered
        return PollResult("completed")

    def fetch(self, remote_id, workdir) -> Output:
        raise ProviderError("motion-still is synchronous; output is returned by submit()")

    def actual_cost(self, request, usage) -> Tuple[float, str]:
        return 0.0, "local ffmpeg (no API call)"
