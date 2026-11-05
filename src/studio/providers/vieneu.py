#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Copyright (c) 2026 Sixun Dong
# Author: Sixun Dong
# Licensed under the Apache License, Version 2.0
"""
VieNeuTTSProvider — Vietnamese speech from VieNeu-TTS v3 Turbo (Apache-2.0, on-device, 48 kHz), $0.

Two routes, same request:
  route: sdk     (default) in-process `from vieneu import Vieneu` (`pip install vieneu`; CPU runs torch-free on
                 ONNX Runtime, RTF ~0.5 on a desktop CPU per the project's README; GPU ~0.02 batched). The engine
                 is loaded once per worker process and reused. First use downloads the weights from Hugging Face.
  route: server  a running `python -m apps.openai_speech` from the VieNeu repo (OpenAI-shaped POST /v1/audio/speech)
                 at VIENEU_BASE_URL. Its WAV header claims an unknown length (it streams), so the file is re-muxed
                 with ffmpeg before the studio measures its duration.

Voices: the studio plans OpenAI-style voice names (alloy / verse / sage ...); providers file `tts.voice_map`
translates each to a VieNeu preset name (e.g. "Hải Đăng", "Ngọc Huyền") or to `clone:<path to a 3-8 s clip>`
(instant voice cloning; keep one clip per character for a stable voice across the whole series). An unmapped
voice is refused before any synthesis. Free-text `instructions` have no VieNeu equivalent (the reading style
follows the reference voice) and are dropped with an adaptation note; VieNeu's inline cues ([cười], [thở dài],
[hắng giọng]) can be written in the dialogue text itself.

Docs: https://github.com/pnnbao97/VieNeu-TTS (README, apps/openai_speech.py), PyPI `vieneu` 3.8.x.
"""

import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .. import avtools
from .base import NotConfigured, Output, Provider, ProviderCapabilities, ProviderError, RateLimited, SubmitResult, \
    UnsupportedParameter

_ENGINES: Dict[Tuple, Any] = {}
_LOCK = threading.Lock()


def _engine(mode: str, kwargs: Dict[str, Any]):
    """One VieNeu engine per (mode, options) per process: loading takes seconds, synthesis is serialized."""
    key = (mode, tuple(sorted(kwargs.items())))
    with _LOCK:
        if key not in _ENGINES:
            try:
                from vieneu import Vieneu
            except ImportError as e:
                raise NotConfigured("VieNeu-TTS is not installed: pip install vieneu") from e
            _ENGINES[key] = Vieneu(mode=mode, **kwargs)
        return _ENGINES[key]


class VieNeuTTSProvider(Provider):
    name, kind = "vieneu-tts", "tts"
    caps = ProviderCapabilities(
        kind="tts", accepted_fields=("text", "voice", "format", "language"),
        max_text_chars=20000, idempotency=True, usage_reporting=True,
        constraints=["voice = VieNeu preset name or clone:<clip.wav> via providers file tts.voice_map",
                     "free-text instructions are not supported (dropped, recorded as an adaptation)",
                     "inline cues [cười] [thở dài] [hắng giọng] are read from the text", "local, $0"],
        docs="https://github.com/pnnbao97/VieNeu-TTS",
    )

    def __init__(self, model: str = "v3turbo", cfg: Optional[Dict[str, Any]] = None, price=None):
        super().__init__(price if price is not None else {"unit": "1k_chars", "usd": 0.0})
        self.cfg = cfg or {}
        self.model = model or "v3turbo"
        self.route = self.cfg.get("route", "sdk")
        self.base_url = (os.getenv(self.cfg.get("base_url_env", "VIENEU_BASE_URL")) or "").rstrip("/")
        self.api_key = "local" if self.route == "sdk" else (os.getenv("VIENEU_API_KEY") or "local")
        self.engine_kwargs = {k: v for k, v in (self.cfg.get("engine") or {}).items() if v is not None}
        self._transport = None             # tests inject an httpx.MockTransport for the server route
        self._sdk = None                   # tests inject a fake engine for the sdk route

    # ── voices ────────────────────────────────────────────────────────────
    def _voice(self, planned: str) -> Dict[str, Any]:
        vmap = self.cfg.get("voice_map") or {}
        target = vmap.get(planned, planned if planned in (self.cfg.get("voices") or []) else None)
        if not target:
            raise UnsupportedParameter(f"vieneu-tts: voice {planned!r} is not mapped (providers file tts.voice_map)")
        if str(target).startswith("clone:"):
            clip = Path(str(target)[6:]).expanduser()
            if not clip.is_absolute():
                clip = Path(__file__).resolve().parents[3] / clip
            if not clip.exists():
                raise UnsupportedParameter(f"vieneu-tts: reference clip for {planned!r} not found: {clip}")
            return {"ref_audio": str(clip), "label": f"clone:{clip.name}"}
        return {"voice": str(target), "label": str(target)}

    def adapt(self, request):
        req, notes = super().adapt(request)
        if req.get("voice"):
            try:
                notes.append(f"voice {req['voice']} -> {self._voice(req['voice'])['label']} (voice_map)")
            except UnsupportedParameter:
                pass                       # validate() refuses it with the reason
        return req, notes

    def validate(self, request):
        super().validate(request)
        self._voice(request.get("voice", ""))
        lang = (request.get("language") or "vi").split("-")[0]
        if lang not in ("vi", "en"):
            raise UnsupportedParameter(f"vieneu-tts speaks Vietnamese and English, not {lang!r}")

    # ── synthesis ─────────────────────────────────────────────────────────
    def submit(self, request, idempotency_key, workdir):
        self.validate(request)
        v = self._voice(request["voice"])
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        out = workdir / f"vieneu_{idempotency_key[:12]}.wav"
        if self.route == "server":
            self._via_server(request["text"], v, out)
        else:
            self._via_sdk(request["text"], v, out)
        info = avtools.probe(out)
        if not info["duration"]:
            raise ProviderError("vieneu-tts: empty audio")
        return SubmitResult(f"local_{idempotency_key[:12]}",
                            Output(out, "audio/wav", usage={"characters": len(request["text"])},
                                   meta={"voice": v["label"], "route": self.route}))

    def _via_sdk(self, text: str, v: Dict[str, Any], out: Path) -> None:
        tts = self._sdk or _engine(self.model, self.engine_kwargs)
        kw = {"ref_audio": v["ref_audio"]} if "ref_audio" in v else {"voice": v["voice"]}
        try:
            with _LOCK:                    # one synthesis at a time per process (CPU engine shares the cores)
                audio = tts.infer(text, **kw)
                tts.save(audio, str(out))
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"vieneu-tts: {type(e).__name__}: {e}") from e

    def _via_server(self, text: str, v: Dict[str, Any], out: Path) -> None:
        if not self.base_url:
            raise NotConfigured("VIENEU_BASE_URL is not set (route: server)")
        if "ref_audio" in v:
            raise UnsupportedParameter("vieneu-tts server route: clone voices must be enrolled on the server "
                                       "(POST /v1/voices) and mapped by name")
        import httpx
        raw = out.with_suffix(".stream.wav")
        body = {"model": "vieneu-v3-turbo", "input": text, "voice": v["voice"], "response_format": "wav"}
        try:
            with httpx.Client(timeout=600, transport=self._transport,
                              headers={"Authorization": f"Bearer {self.api_key}"}) as c:
                r = c.post(f"{self.base_url}/audio/speech", json=body)
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise ProviderError(f"vieneu-tts server: {type(e).__name__}: {e}", retryable=True) from e
        if r.status_code == 429:
            raise RateLimited("vieneu-tts server busy", retry_after=float(r.headers.get("retry-after") or 2))
        if r.status_code >= 400:
            raise ProviderError(f"vieneu-tts server {r.status_code}: {r.text[:300]}", retryable=r.status_code >= 500)
        raw.write_bytes(r.content)
        try:   # the streamed header claims an unknown length: rewrite a proper WAV
            avtools.run(["-i", str(raw), "-c:a", "pcm_s16le", str(out)])
        finally:
            raw.unlink(missing_ok=True)

    def actual_cost(self, request, usage):
        return 0.0, "local VieNeu-TTS (no API call)"
