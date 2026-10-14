#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Copyright (c) 2026 Sixun Dong
# Author: Sixun Dong
# Licensed under the Apache License, Version 2.0
"""
Live (paid) provider adapters. Each one follows the provider's official documentation, read on
2026-10-14 (URLs in `caps.docs`), and declares exactly the fields it sends.

  OpenAIImageProvider        gpt-image-1 via /images/generations and /images/edits (references)
  VeoVideoProvider           Gemini API Veo 3.1 (predictLongRunning + operation polling)
  OpenAICompatVideoProvider  any self-hosted OpenAI-shaped /videos server (e.g. a Wan 2.2 wrapper);
                             its optional fields are whatever providers.yaml declares, nothing more.
                             (OpenAI's own Sora Videos API was shut down on 2026-09-24.)
  OpenAITTSProvider          /audio/speech (gpt-4o-mini-tts), WAV out, duration measured afterwards
  VLMJudgeProvider           VisualJudgeAgent over a vision chat model, with the canonical reference
                             images attached as labelled pictures

None of these is exercised by the offline test suite against a real endpoint; contract tests use
stub transports. Live use needs credentials AND a non-zero project budget.
"""

import base64
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .. import avtools
from .base import (
    AmbiguousSubmission, NotConfigured, Output, PollResult, Provider, ProviderCapabilities, ProviderError, RateLimited,
    SubmitResult,
)


def _classify_openai_error(e: Exception) -> ProviderError:
    """Map openai SDK exceptions onto the studio's error classes."""
    name = type(e).__name__
    if name == "RateLimitError":
        ra = None
        try:
            ra = float(e.response.headers.get("retry-after"))  # type: ignore[attr-defined]
        except Exception:
            pass
        return RateLimited(str(e), retry_after=ra)
    if name in ("APITimeoutError", "APIConnectionError"):
        # The request may have reached the server: never assume it did not.
        return AmbiguousSubmission(f"{name}: {e}")
    if name in ("InternalServerError",):
        return ProviderError(f"{name}: {e}", retryable=True)
    return ProviderError(f"{name}: {e}", retryable=False)


# ── image ──────────────────────────────────────────────────────────────────

class OpenAIImageProvider(Provider):
    name, kind = "openai-image", "image"
    caps = ProviderCapabilities(
        kind="image", accepted_fields=("prompt", "size", "reference_images", "quality"),
        sizes=["1024x1024", "1536x1024", "1024x1536"], max_reference_images=16, negative_prompt=False, seed=False,
        idempotency=False, cancellation=False, usage_reporting=True, async_polling=False,
        constraints=["no seed / negative prompt", "always returns base64", "references go to /images/edits (PNG/WebP/JPG < 50MB)"],
        docs="https://developers.openai.com/api/reference/python/resources/images/methods/edit",
    )

    def __init__(self, model: str = "gpt-image-1", api_key: str = "", base_url: Optional[str] = None, price=None):
        super().__init__(price)
        self.model, self.api_key, self.base_url = model, api_key, base_url

    def _client(self):
        if not self.api_key:
            raise NotConfigured("T2I_API_KEY / LLM_API_KEY is not set")
        from openai import OpenAI
        return OpenAI(api_key=self.api_key, base_url=self.base_url, max_retries=0, timeout=300)

    def submit(self, request, idempotency_key, workdir):
        self.validate(request)
        client = self._client()
        kw: Dict[str, Any] = dict(model=self.model, prompt=request["prompt"], n=1, size=request.get("size", "1024x1536"))
        if request.get("quality"):
            kw["quality"] = request["quality"]
        try:
            refs = request.get("reference_images") or []
            if refs:
                handles = [open(r["path"], "rb") for r in refs]
                try:
                    resp = client.images.edit(image=handles, **kw)
                finally:
                    for h in handles:
                        h.close()
            else:
                resp = client.images.generate(**kw)
        except Exception as e:  # noqa: BLE001
            raise _classify_openai_error(e) from e
        data = resp.data[0]
        if not getattr(data, "b64_json", None):
            raise ProviderError("images API returned no b64_json")
        workdir.mkdir(parents=True, exist_ok=True)
        out = workdir / f"img_{idempotency_key[:12]}.png"
        out.write_bytes(base64.b64decode(data.b64_json))
        usage = resp.usage.model_dump() if getattr(resp, "usage", None) else {}
        return SubmitResult(f"sync_{idempotency_key[:12]}", Output(out, "image/png", usage=usage,
                                                                     meta={"revised_prompt": getattr(data, "revised_prompt", None)}))

    def actual_cost(self, request, usage) -> Tuple[Optional[float], str]:
        p_in, p_out = self.price.get("input_usd_per_1m_tokens"), self.price.get("output_usd_per_1m_tokens")
        if usage and p_in is not None and p_out is not None:
            usd = usage.get("input_tokens", 0) / 1e6 * p_in + usage.get("output_tokens", 0) / 1e6 * p_out
            return round(usd, 6), "provider usage tokens x configured token price"
        return super().actual_cost(request, usage)


# ── video: Veo (Gemini API) ────────────────────────────────────────────────

class VeoVideoProvider(Provider):
    name, kind = "veo", "video"
    caps = ProviderCapabilities(
        kind="video", accepted_fields=("prompt", "duration", "size", "seed", "image", "reference_images", "negative_prompt"),
        sizes=["1280x720", "720x1280", "1920x1080", "1080x1920"], durations=[4.0, 6.0, 8.0], max_reference_images=3,
        first_frame_image=True, generates_audio=True, lip_sync=False, negative_prompt=True, seed=True,
        idempotency=False, cancellation=False, usage_reporting=False, async_polling=True,
        constraints=["aspect 16:9 or 9:16 only", "1080p and reference images require 8 s",
                     "audio is always generated natively (not driven by our TTS track: no lip sync to it)",
                     "videos are kept server-side for 2 days", "seed improves but does not guarantee determinism"],
        docs="https://ai.google.dev/gemini-api/docs/veo",
    )
    BASE = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, model: str = "veo-3.1-generate-preview", api_key: str = "", price=None, transport=None):
        super().__init__(price)
        self.model, self.api_key = model, api_key
        self._transport = transport  # tests inject an httpx.MockTransport

    def adapt(self, request):
        req, notes = super().adapt(request)
        if (req.get("reference_images") or req.get("size", "").startswith(("1920", "1080"))) and req.get("duration") != 8.0:
            notes.append(f"duration {req.get('duration')}s -> 8s (Veo requires 8 s with references / 1080p)")
            req["duration"] = 8.0
        return req, notes

    def _http(self):
        if not self.api_key:
            raise NotConfigured("VEO_API_KEY / GEMINI_API_KEY is not set")
        import httpx
        return httpx.Client(timeout=120, headers={"x-goog-api-key": self.api_key}, transport=self._transport,
                            follow_redirects=True)

    @staticmethod
    def _inline(path: str) -> Dict[str, Any]:
        mime = "image/jpeg" if path.lower().endswith((".jpg", ".jpeg")) else "image/png"
        return {"inlineData": {"mimeType": mime, "data": base64.b64encode(Path(path).read_bytes()).decode()}}

    def submit(self, request, idempotency_key, workdir):
        self.validate(request)
        w, h = (int(v) for v in request.get("size", "720x1280").split("x"))
        inst: Dict[str, Any] = {"prompt": request["prompt"]}
        if request.get("image"):
            inst["image"] = self._inline(request["image"])
        if request.get("reference_images"):
            inst["referenceImages"] = [{"image": self._inline(r["path"]), "referenceType": "asset"}
                                       for r in request["reference_images"]]
        params: Dict[str, Any] = {
            "aspectRatio": "9:16" if h > w else "16:9",
            "resolution": "1080p" if max(w, h) >= 1920 else "720p",
            "durationSeconds": str(int(request.get("duration", 8))),
            "numberOfVideos": 1,
            "personGeneration": "allow_adult" if (request.get("image") or request.get("reference_images")) else "allow_all",
        }
        if request.get("seed") is not None:
            params["seed"] = int(request["seed"])
        if request.get("negative_prompt"):
            params["negativePrompt"] = request["negative_prompt"]
        import httpx
        try:
            with self._http() as c:
                r = c.post(f"{self.BASE}/models/{self.model}:predictLongRunning",
                           json={"instances": [inst], "parameters": params})
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise AmbiguousSubmission(f"veo submit: {type(e).__name__}: {e}") from e
        if r.status_code == 429:
            raise RateLimited("veo: 429", retry_after=_retry_after(r))
        if r.status_code >= 500:
            raise ProviderError(f"veo submit {r.status_code}: {r.text[:300]}", retryable=True)
        if r.status_code >= 400:
            raise ProviderError(f"veo submit {r.status_code}: {r.text[:300]}")
        return SubmitResult(r.json()["name"])

    def poll(self, remote_id):
        with self._http() as c:
            r = c.get(f"{self.BASE}/{remote_id}")
        if r.status_code == 429:
            return PollResult("running", error="poll rate-limited")
        if r.status_code >= 400:
            return PollResult("unknown", error=f"poll {r.status_code}: {r.text[:200]}")
        d = r.json()
        if not d.get("done"):
            return PollResult("running")
        if d.get("error"):
            return PollResult("failed", error=str(d["error"].get("message", d["error"]))[:500])
        return PollResult("completed")

    def fetch(self, remote_id, workdir):
        with self._http() as c:
            d = c.get(f"{self.BASE}/{remote_id}").json()
            samples = d.get("response", {}).get("generateVideoResponse", {}).get("generatedSamples") or []
            if not samples:
                raise ProviderError("veo: completed operation has no generatedSamples (possibly filtered)")
            uri = samples[0]["video"]["uri"]
            r = c.get(uri)
            r.raise_for_status()
        workdir.mkdir(parents=True, exist_ok=True)
        out = workdir / f"veo_{remote_id.rsplit('/', 1)[-1]}.mp4"
        out.write_bytes(r.content)
        info = avtools.probe(out)
        return Output(out, "video/mp4", usage={"seconds": info["duration"]}, meta=info)


def _retry_after(r) -> Optional[float]:
    try:
        return float(r.headers.get("retry-after"))
    except (TypeError, ValueError):
        return None


# ── video: generic OpenAI-shaped self-hosted server ────────────────────────

class OpenAICompatVideoProvider(Provider):
    """For a server you run (Wan 2.2 / LTX wrapper). Its extra fields must be declared in providers.yaml."""

    name, kind = "openai-compatible-video", "video"

    def __init__(self, model: str, base_url: str, api_key: str = "EMPTY", caps: Optional[Dict[str, Any]] = None,
                 price=None):
        super().__init__(price)
        caps = caps or {}
        self.model, self.base_url, self.api_key = model, base_url, api_key
        self.caps = ProviderCapabilities(
            kind="video",
            accepted_fields=tuple(["prompt", "duration", "size", "image"] + list(caps.get("extra_fields", []))),
            sizes=caps.get("sizes"), durations=caps.get("durations"),
            min_duration=caps.get("min_duration", 1.0), max_duration=caps.get("max_duration", 10.0),
            first_frame_image=True, generates_audio=bool(caps.get("generates_audio", False)),
            negative_prompt="negative_prompt" in caps.get("extra_fields", []),
            seed="seed" in caps.get("extra_fields", []), idempotency=False, cancellation=False,
            usage_reporting=False, async_polling=True,
            constraints=["only fields listed in providers.yaml extra_fields are sent in extra_body"],
            docs="OpenAI Videos API shape (create / retrieve / download_content)",
        )

    def _client(self):
        if not self.base_url:
            raise NotConfigured("VIDEO_BASE_URL is not set")
        from openai import OpenAI
        return OpenAI(api_key=self.api_key or "EMPTY", base_url=self.base_url, max_retries=0, timeout=120)

    def submit(self, request, idempotency_key, workdir):
        self.validate(request)
        extra = {k: request[k] for k in self.caps.accepted_fields
                 if k not in ("prompt", "duration", "size", "image") and request.get(k) is not None}
        kw: Dict[str, Any] = dict(model=self.model, prompt=request["prompt"], size=request.get("size"),
                                  seconds=str(int(round(float(request.get("duration", 4))))), extra_body=extra)
        try:
            if request.get("image"):
                with open(request["image"], "rb") as f:
                    v = self._client().videos.create(input_reference=f, **kw)
            else:
                v = self._client().videos.create(**kw)
        except Exception as e:  # noqa: BLE001
            raise _classify_openai_error(e) from e
        return SubmitResult(v.id)

    def poll(self, remote_id):
        try:
            v = self._client().videos.retrieve(remote_id)
        except Exception as e:  # noqa: BLE001
            return PollResult("unknown", error=str(e)[:200])
        st = getattr(v, "status", "")
        if st == "completed":
            return PollResult("completed")
        if st == "failed":
            err = getattr(v, "error", None)
            return PollResult("failed", error=getattr(err, "message", None) or str(err))
        return PollResult("running", progress=getattr(v, "progress", None))

    def fetch(self, remote_id, workdir):
        workdir.mkdir(parents=True, exist_ok=True)
        out = workdir / f"vid_{remote_id[-16:]}.mp4"
        self._client().videos.download_content(remote_id).write_to_file(str(out))
        info = avtools.probe(out)
        return Output(out, "video/mp4", usage={"seconds": info["duration"]}, meta=info)


# ── TTS ────────────────────────────────────────────────────────────────────

class OpenAITTSProvider(Provider):
    name, kind = "openai-tts", "tts"
    caps = ProviderCapabilities(
        kind="tts", accepted_fields=("text", "voice", "instructions", "speed", "format"),
        max_text_chars=4096,
        voices=["alloy", "ash", "ballad", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer", "verse",
                "marin", "cedar"],
        idempotency=False, usage_reporting=False,
        constraints=["instructions not supported by tts-1 / tts-1-hd", "speed 0.25-4.0", "input <= 4096 chars",
                     "duration is measured from the returned waveform"],
        docs="https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create",
    )

    def __init__(self, model: str = "gpt-4o-mini-tts", api_key: str = "", base_url: Optional[str] = None, price=None):
        super().__init__(price)
        self.model, self.api_key, self.base_url = model, api_key, base_url

    def submit(self, request, idempotency_key, workdir):
        self.validate(request)
        if not self.api_key:
            raise NotConfigured("TTS_API_KEY / LLM_API_KEY is not set")
        from openai import OpenAI
        client = OpenAI(api_key=self.api_key, base_url=self.base_url, max_retries=0, timeout=120)
        kw: Dict[str, Any] = dict(model=self.model, voice=request["voice"], input=request["text"], response_format="wav")
        if request.get("instructions") and not self.model.startswith("tts-1"):
            kw["instructions"] = request["instructions"]
        if request.get("speed"):
            kw["speed"] = float(request["speed"])
        try:
            resp = client.audio.speech.create(**kw)
        except Exception as e:  # noqa: BLE001
            raise _classify_openai_error(e) from e
        workdir.mkdir(parents=True, exist_ok=True)
        out = workdir / f"tts_{idempotency_key[:12]}.wav"
        resp.write_to_file(str(out))
        return SubmitResult(f"sync_{idempotency_key[:12]}", Output(out, "audio/wav", usage={"characters": len(request["text"])}))


# ── reviewer ───────────────────────────────────────────────────────────────

class VLMJudgeProvider(Provider):
    name, kind = "vlm-judge", "judge"
    caps = ProviderCapabilities(
        kind="judge", accepted_fields=("media", "references", "prompt", "shot", "media_kind", "global_style",
                                       "shot_context"),
        max_reference_images=8, usage_reporting=False,
        constraints=["vision chat model behind LLM_*; canonical references are sent as images, labelled by asset id"],
        docs="src/agents/dailies_reviewer/visual_judge.yaml",
    )

    def __init__(self, model: Optional[str] = None, price=None):
        super().__init__(price)
        self.model = model or os.getenv("JUDGE_MODEL") or os.getenv("LLM_MODEL") or "gpt-5"

    def submit(self, request, idempotency_key, workdir):
        from ...agents.dailies_reviewer.visual_judge import VisualJudgeAgent
        from ...config import Config
        from ...schemas.blueprint import ShotBlueprint
        if not Config.LLM_API_KEY:
            raise NotConfigured("LLM_API_KEY is not set (the visual judge is a vision chat model)")
        shot = ShotBlueprint.model_validate(request["shot"])
        refs: Dict[str, list] = {}
        for r in request.get("references") or []:
            refs.setdefault(r["label"], []).append(r["path"])
        judge = VisualJudgeAgent()
        try:
            if request.get("media_kind") == "clip":
                review = judge.review_clip(request["media"], shot, request["prompt"], request.get("global_style", ""),
                                           reference_images=refs)
            else:
                review = judge.review_keyframe(request["media"][0], shot, request["prompt"],
                                               request.get("global_style", ""), used_references=bool(refs),
                                               reference_images=refs)
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"judge: {type(e).__name__}: {e}") from e
        return SubmitResult(f"jdg_{idempotency_key[:10]}", Output(Path(os.devnull), "application/json",
                                                                  meta={"review": review.model_dump()}))
