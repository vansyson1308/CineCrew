#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Copyright (c) 2026 Sixun Dong
# Author: Sixun Dong
# Licensed under the Apache License, Version 2.0
"""
ComfyUIVideoProvider — any open-weight video model you run in ComfyUI (your own GPU or a rented one), driven by
a workflow you export from ComfyUI ("Save (API)"), with placeholders where the studio puts its inputs.

No model-specific code: the model lives in the workflow. Typical workflows:
  talking   image + speech -> lip-synced clip  (InfiniteTalk / MultiTalk, LongCat-Video-Avatar, Kandinsky S2V ...)
  action    image + prompt -> clip              (Wan 2.2 I2V, LTX-2.x, HunyuanVideo 1.5 ...)

Placeholders (a JSON string value that is exactly the placeholder becomes the typed value; inside a longer
string it is substituted as text):
  {{image}} {{audio}}             uploaded file names (ComfyUI input folder) of the keyframe / lip-sync track
  {{prompt}} {{negative_prompt}}  text
  {{seed}} {{width}} {{height}} {{fps}} {{frames}} {{duration}}   numbers (frames follow `frames_rule`)

API used (ComfyUI server routes): POST /upload/image (multipart, also used for audio files), POST /prompt,
GET /history/{prompt_id}, GET /view, POST /queue {"delete": [...]} to cancel a queued prompt.
"""

import copy
import json
import math
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from .. import avtools
from .base import (
    AmbiguousSubmission, NotConfigured, Output, PollResult, Provider, ProviderCapabilities, ProviderError, RateLimited,
    SubmitResult,
)

_ROOT = Path(__file__).resolve().parents[3]
_PH = re.compile(r"\{\{(\w+)\}\}")
VIDEO_EXT = (".mp4", ".webm", ".mov", ".mkv", ".gif")


def frames_for(duration: float, fps: int, rule: Optional[str]) -> int:
    """Frame count for a clip of `duration` seconds; `rule` '4n+1' (Wan family) / '8n+1' (LTX) rounds UP to fit."""
    n = max(1, int(math.ceil(duration * fps - 1e-6)))
    m = re.fullmatch(r"(\d+)n\+(\d+)", (rule or "").replace(" ", ""))
    if m:
        k, b = int(m.group(1)), int(m.group(2))
        n = b + k * max(0, math.ceil((n - b) / k))
    return n


def fill(workflow: Any, values: Dict[str, Any]) -> Any:
    """Replace {{name}} placeholders throughout an API-format workflow. Unknown placeholders raise."""
    def sub(v):
        if isinstance(v, dict):
            return {k: sub(x) for k, x in v.items()}
        if isinstance(v, list):
            return [sub(x) for x in v]
        if isinstance(v, str):
            whole = _PH.fullmatch(v)
            if whole:
                if whole.group(1) not in values:
                    raise NotConfigured(f"workflow placeholder {v} has no value")
                return values[whole.group(1)]

            def one(m):
                if m.group(1) not in values:
                    raise NotConfigured(f"workflow placeholder {m.group(0)} has no value")
                return str(values[m.group(1)])
            return _PH.sub(one, v)
        return v
    return sub(copy.deepcopy(workflow))


def placeholders(workflow: Any) -> set:
    return set(_PH.findall(json.dumps(workflow)))


class ComfyUIVideoProvider(Provider):
    """One ComfyUI workflow = one provider instance. `cfg` keys: workflow (path), base_url_env (COMFYUI_BASE_URL),
    fps, frames_rule, min_duration, max_duration, lip_sync, label (route name), timeout, output_node."""

    name, kind = "comfyui", "video"

    def __init__(self, model: str, cfg: Dict[str, Any], price=None, transport=None):
        super().__init__(price)
        self.cfg = cfg or {}
        self.model = model or Path(str(self.cfg.get("workflow", "workflow"))).stem
        self.base_url = (os.getenv(self.cfg.get("base_url_env", "COMFYUI_BASE_URL")) or "").rstrip("/")
        self.api_key = self.base_url and "local"
        self.fps = int(self.cfg.get("fps", 25))
        self._transport = transport
        self._workflow: Optional[Dict[str, Any]] = None
        wf = self._load(required=False)
        ph = placeholders(wf) if wf else {"image", "prompt"}
        fields = ["prompt", "duration", "size", "image", "seed", "negative_prompt", "label"]
        if "audio" in ph:
            fields.append("audio")
        self.lip_sync = bool(self.cfg.get("lip_sync", "audio" in ph))
        self.caps = ProviderCapabilities(
            kind="video", accepted_fields=tuple(fields),
            min_duration=float(self.cfg.get("min_duration", 1.0)), max_duration=float(self.cfg.get("max_duration", 5.0)),
            first_frame_image=True, generates_audio=bool(self.cfg.get("generates_audio", False)), lip_sync=self.lip_sync,
            negative_prompt="negative_prompt" in ph, seed="seed" in ph, idempotency=False, cancellation=True,
            usage_reporting=False, async_polling=True,
            constraints=[f"ComfyUI workflow {self.cfg.get('workflow')}", f"frames rule {self.cfg.get('frames_rule') or 'none'}",
                         "lip sync to the shot's dialogue track" if self.lip_sync else "silent clip (studio mixes audio)"],
            docs="docs/studio/MOTION_LIPSYNC.md",
        )

    # ── workflow ──────────────────────────────────────────────────────────
    def _load(self, required: bool = True) -> Optional[Dict[str, Any]]:
        if self._workflow is not None:
            return self._workflow
        p = Path(str(self.cfg.get("workflow") or ""))
        if p and not p.is_absolute():
            p = _ROOT / p
        if not self.cfg.get("workflow") or not p.is_file():
            if required:
                raise NotConfigured(f"ComfyUI workflow not found: {p} — export yours from ComfyUI with 'Save (API)' and "
                                    "add the placeholders (docs/studio/MOTION_LIPSYNC.md)")
            return None
        self._workflow = json.loads(p.read_text(encoding="utf-8"))
        return self._workflow

    def _http(self, timeout: float = 120):
        if not self.base_url:
            raise NotConfigured(f"{self.cfg.get('base_url_env', 'COMFYUI_BASE_URL')} is not set (your ComfyUI server)")
        import httpx
        return httpx.Client(timeout=timeout, transport=self._transport, follow_redirects=True)

    def _upload(self, c, path: str) -> str:
        p = Path(path)
        r = c.post(f"{self.base_url}/upload/image", files={"image": (p.name, p.read_bytes())},
                   data={"type": "input", "overwrite": "true"})
        if r.status_code >= 400:
            raise ProviderError(f"comfyui upload {r.status_code}: {r.text[:200]}", retryable=r.status_code >= 500)
        d = r.json()
        return f"{d['subfolder']}/{d['name']}" if d.get("subfolder") else d["name"]

    # ── lifecycle ─────────────────────────────────────────────────────────
    def submit(self, request, idempotency_key, workdir):
        self.validate(request)
        wf = self._load()
        need = placeholders(wf)
        if "audio" in need and not request.get("audio"):
            raise ProviderError(f"{self.model}: this workflow lip-syncs and needs the shot's dialogue track", retryable=False)
        w, h = (int(v) for v in str(request.get("size", "720x1280")).lower().split("x"))
        dur = float(request.get("duration", 4))
        import httpx
        try:
            with self._http() as c:
                values = {"prompt": request.get("prompt", ""), "negative_prompt": request.get("negative_prompt") or "",
                          "seed": int(request.get("seed") or 0), "width": w, "height": h, "fps": self.fps,
                          "duration": dur, "frames": frames_for(dur, self.fps, self.cfg.get("frames_rule"))}
                if "image" in need:
                    values["image"] = self._upload(c, request["image"])
                if "audio" in need:
                    values["audio"] = self._upload(c, request["audio"])
                body = {"prompt": fill(wf, values), "client_id": f"cinecrew-{idempotency_key[:16]}"}
                r = c.post(f"{self.base_url}/prompt", json=body)
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise AmbiguousSubmission(f"comfyui submit: {type(e).__name__}: {e}") from e
        if r.status_code == 429:
            raise RateLimited("comfyui: 429")
        if r.status_code >= 500:
            raise ProviderError(f"comfyui submit {r.status_code}: {r.text[:300]}", retryable=True)
        if r.status_code >= 400:
            raise ProviderError(f"comfyui rejected the workflow {r.status_code}: {r.text[:500]}")
        d = r.json()
        if d.get("node_errors"):
            raise ProviderError(f"comfyui node errors: {json.dumps(d['node_errors'])[:500]}")
        return SubmitResult(d["prompt_id"])

    def _history(self, prompt_id: str) -> Dict[str, Any]:
        with self._http() as c:
            r = c.get(f"{self.base_url}/history/{prompt_id}")
        if r.status_code >= 400:
            raise ProviderError(f"comfyui history {r.status_code}", retryable=True)
        return (r.json() or {}).get(prompt_id) or {}

    def poll(self, remote_id):
        try:
            h = self._history(remote_id)
        except Exception as e:  # noqa: BLE001
            return PollResult("unknown", error=str(e)[:200])
        if not h:
            return PollResult("running")           # queued or running: history appears when it finishes
        st = h.get("status") or {}
        if st.get("status_str") == "error":
            msg = next((m[1].get("exception_message") for m in st.get("messages", [])
                        if m and m[0] == "execution_error"), None)
            return PollResult("failed", error=(msg or "execution error")[:500])
        if st.get("completed") or st.get("status_str") == "success":
            return PollResult("completed")
        return PollResult("running")

    def _output_file(self, h: Dict[str, Any]) -> Dict[str, Any]:
        outs = h.get("outputs") or {}
        nodes = [str(self.cfg["output_node"])] if self.cfg.get("output_node") else list(outs)
        for nid in nodes:
            for files in (outs.get(nid) or {}).values():
                for f in files if isinstance(files, list) else []:
                    if isinstance(f, dict) and str(f.get("filename", "")).lower().endswith(VIDEO_EXT):
                        return f
        raise ProviderError("comfyui: the finished workflow produced no video file (check its save node)")

    def fetch(self, remote_id, workdir):
        f = self._output_file(self._history(remote_id))
        with self._http(timeout=600) as c:
            r = c.get(f"{self.base_url}/view", params={"filename": f["filename"], "subfolder": f.get("subfolder", ""),
                                                       "type": f.get("type", "output")})
            r.raise_for_status()
        Path(workdir).mkdir(parents=True, exist_ok=True)
        out = Path(workdir) / f"comfy_{remote_id[:12]}_{uuid.uuid4().hex[:4]}{Path(f['filename']).suffix}"
        out.write_bytes(r.content)
        info = avtools.probe(out)
        return Output(out, "video/mp4", usage={"seconds": info["duration"]}, meta={**info, "workflow": self.model})

    def cancel(self, remote_id):
        try:
            with self._http() as c:
                c.post(f"{self.base_url}/queue", json={"delete": [remote_id]})
            return True
        except Exception:  # noqa: BLE001
            return False
