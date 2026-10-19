#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Copyright (c) 2026 Sixun Dong
# Author: Sixun Dong
# Licensed under the Apache License, Version 2.0
"""
Multi-reference video / image providers: the backend line-up of the huobao-drama project
(github.com/chatfire-AI/huobao-drama), re-implemented here from the providers' public API documentation.
huobao-drama is CC BY-NC-SA 4.0, which is incompatible with this Apache-2.0 repository, so no code,
prompt or text was copied from it: only the choice of backends, their documented request shapes and the
idea of feeding every asset sheet to the video model as a labelled reference image.

  SeedanceVideoProvider   ByteDance Seedance 2.0 (Volcengine Ark / BytePlus ModelArk) — /contents/generations/tasks
  MiniMaxVideoProvider    MiniMax H3 — /v2/video_generation + /v2/query/video_generation/{id}
  WanVideoProvider        Alibaba Model Studio Wan 3.0 — DashScope async video-synthesis + /tasks/{id}
  SeedreamImageProvider   ByteDance Seedream (Ark /images/generations), up to 10 reference images
  GeminiImageProvider     Gemini image models (generateContent, inline reference images)
  MiniMaxTTSProvider      MiniMax T2A v2 (/v1/t2a_v2), hex-encoded audio, Vietnamese language boost

Every adapter talks plain HTTPS through httpx (tests inject an httpx.MockTransport) and can be pointed at
the provider directly or at the Huobao/Firemux gateway, which serves the same request shapes under one key
(see `resolve_endpoint`). Reference and first-frame images are sent inline as base64 data URIs, so no public
file server is needed. Video models that also accept reference images get the canonical sheets of every
asset in the shot (characters, location, props), labelled in the prompt as [Image N] = name.
"""

import base64
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import avtools
from ..hashing import hash_file
from .base import (
    AmbiguousSubmission, NotConfigured, Output, PollResult, Provider, ProviderCapabilities, ProviderError, RateLimited,
    SubmitResult, UnsupportedParameter,
)

HUOBAO_DEFAULT_BASE = "https://api.firemux.com"


# ── endpoint / key resolution ───────────────────────────────────────────────

def resolve_endpoint(cfg: Dict[str, Any], default_base: str, key_envs: Tuple[str, ...],
                     gateway_path: str) -> Tuple[str, str, str]:
    """
    (base_url, api_key, route) for one provider entry of providers.yaml.

    Direct credentials win: the provider's own key env (or cfg.api_key_env) with cfg.base_url / base_url_env
    or the documented default base. Otherwise HUOBAO_API_KEY routes through the gateway
    (HUOBAO_BASE_URL, default https://api.firemux.com) + the provider's gateway path.
    route is "direct", "huobao-gateway" or "unconfigured".
    """
    envs = tuple(filter(None, [cfg.get("api_key_env")])) + key_envs
    key = next((os.environ[e] for e in envs if os.getenv(e)), "")
    if key:
        base = os.getenv(cfg.get("base_url_env") or "", "") or cfg.get("base_url") or default_base
        return base.rstrip("/"), key, "direct"
    hb = os.getenv("HUOBAO_API_KEY", "")
    if hb and cfg.get("gateway", True):
        base = (os.getenv("HUOBAO_BASE_URL") or HUOBAO_DEFAULT_BASE).rstrip("/")
        return base + gateway_path, hb, "huobao-gateway"
    return (cfg.get("base_url") or default_base).rstrip("/"), "", "unconfigured"


def join_url(base: str, prefix: str, path: str) -> str:
    """base + prefix + path, without doubling a prefix the base already ends with (e.g. '/api/v3')."""
    base = base.rstrip("/")
    if prefix and not base.endswith(prefix):
        base += prefix
    return base + path


def data_uri(path: str) -> str:
    p = Path(path)
    ext = p.suffix.lower().lstrip(".")
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}.get(ext, "image/png")
    return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"


def _wh(size: str) -> Tuple[int, int]:
    w, h = size.lower().split("x")
    return int(w), int(h)


def _ratio(size: Optional[str]) -> Optional[str]:
    if not size:
        return None
    w, h = _wh(size)
    best = min(("16:9", "9:16", "1:1", "4:3", "3:4", "21:9"),
               key=lambda r: abs(int(r.split(":")[0]) / int(r.split(":")[1]) - w / h))
    return best


def reference_header(refs: List[Dict[str, Any]]) -> str:
    """'Reference images: [Image 1] Minh <char_minh>; [Image 2] ...' — ties each picture to an asset."""
    return "Reference images: " + "; ".join(f"[Image {i}] {r.get('label', '')}" for i, r in enumerate(refs, 1)) + "."


def _retry_after(r) -> Optional[float]:
    try:
        return float(r.headers.get("retry-after"))
    except (TypeError, ValueError):
        return None


def _err_text(r) -> str:
    try:
        d = r.json()
    except Exception:  # noqa: BLE001
        return r.text[:300]
    e = d.get("error") if isinstance(d, dict) else None
    if isinstance(e, dict):
        return f"[{e.get('code', '')}] {e.get('message', '')}".strip()[:300]
    if isinstance(d, dict) and (d.get("code") or d.get("message")):
        return f"[{d.get('code', '')}] {d.get('message', '')}"[:300]
    base = (d.get("base_resp") or {}) if isinstance(d, dict) else {}
    if base.get("status_code"):
        return f"[{base.get('status_code')}] {base.get('status_msg', '')}"[:300]
    return r.text[:300]


# ── shared HTTP plumbing ───────────────────────────────────────────────────

class _HttpProvider(Provider):
    """Bearer-token JSON API. Subclasses set DEFAULT_BASE, KEY_ENVS, GATEWAY_PATH and the request shape."""

    DEFAULT_BASE = ""
    KEY_ENVS: Tuple[str, ...] = ()
    GATEWAY_PATH = ""

    def __init__(self, model: str, cfg: Optional[Dict[str, Any]] = None, price=None, transport=None,
                 api_key: Optional[str] = None, base_url: Optional[str] = None):
        super().__init__(price)
        self.cfg = dict(cfg or {})
        self.model = model
        if api_key is not None:                       # explicit (tests)
            self.base_url, self.api_key, self.route = (base_url or self.DEFAULT_BASE).rstrip("/"), api_key, "direct"
        else:
            self.base_url, self.api_key, self.route = resolve_endpoint(self.cfg, self.DEFAULT_BASE, self.KEY_ENVS,
                                                                       self.GATEWAY_PATH)
        self._transport = transport
        self._results: Dict[str, Dict[str, Any]] = {}

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _http(self, timeout: float = 120):
        if not self.api_key:
            raise NotConfigured(f"{self.name}: no API key ({' / '.join(self.KEY_ENVS)} or HUOBAO_API_KEY)")
        import httpx
        return httpx.Client(timeout=timeout, headers=self._headers(), transport=self._transport,
                            follow_redirects=True)

    def _post(self, url: str, body: Dict[str, Any], extra_headers: Optional[Dict[str, str]] = None,
              timeout: float = 120) -> Dict[str, Any]:
        """POST that maps failures onto the studio's error classes (lost connection = ambiguous)."""
        import httpx
        try:
            with self._http(timeout) as c:
                r = c.post(url, json=body, headers=extra_headers or {})
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise AmbiguousSubmission(f"{self.name} submit: {type(e).__name__}: {e}") from e
        if r.status_code == 429:
            raise RateLimited(f"{self.name}: 429 {_err_text(r)}", retry_after=_retry_after(r))
        if r.status_code >= 500:
            raise ProviderError(f"{self.name} {r.status_code}: {_err_text(r)}", retryable=True)
        if r.status_code >= 400:
            raise ProviderError(f"{self.name} {r.status_code}: {_err_text(r)}")
        try:
            return r.json()
        except ValueError as e:
            raise ProviderError(f"{self.name}: response is not JSON: {r.text[:200]}") from e

    def _get_json(self, url: str) -> Tuple[Optional[Dict[str, Any]], Optional[PollResult]]:
        """(json, None) or (None, PollResult) for transport-level trouble while polling."""
        import httpx
        try:
            with self._http(60) as c:
                r = c.get(url)
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            return None, PollResult("unknown", error=f"poll: {type(e).__name__}")
        if r.status_code == 429:
            return None, PollResult("running", error="poll rate-limited")
        if r.status_code >= 400:
            return None, PollResult("unknown", error=f"poll {r.status_code}: {_err_text(r)}")
        return r.json(), None

    def _delete(self, url: str) -> bool:
        import httpx
        try:
            with self._http(60) as c:
                return c.delete(url).status_code < 400
        except (httpx.TimeoutException, httpx.NetworkError, NotConfigured):
            return False

    def _download(self, url: str, out: Path) -> Path:
        import httpx
        out.parent.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=600, transport=self._transport, follow_redirects=True) as c:
            r = c.get(url)            # result URLs are pre-signed: no auth header (and no key leak to a CDN)
        if r.status_code >= 400:
            raise ProviderError(f"{self.name}: download {r.status_code} (result URLs expire; re-poll for a fresh one)",
                                retryable=True)
        out.write_bytes(r.content)
        return out

    def describe(self) -> Dict[str, Any]:
        d = super().describe()
        d.update({"route": self.route, "base_url": self.base_url})
        return d


class _AsyncVideo(_HttpProvider):
    """submit -> task id; poll -> state; fetch -> download the result URL and probe it."""

    kind = "video"
    mixes_first_frame_and_references = False

    @property
    def video_asset_references(self) -> bool:
        """Whether the engine should attach the shot's asset sheets (see Engine._video_references)."""
        if not self.cfg.get("asset_references", True):
            return False
        return self.mixes_first_frame_and_references or self.cfg.get("video_mode", "first_frame") == "references"

    def adapt(self, request):
        req, notes = super().adapt(request)
        refs = req.get("reference_images") or []
        if refs and req.get("image") and not self.mixes_first_frame_and_references:
            mode = self.cfg.get("video_mode", "first_frame")
            if mode == "references":
                notes.append(f"first frame sent as reference [Image {len(refs) + 1}] ({self.name} cannot mix "
                             "first_frame with reference images)")
                if len(refs) >= self.caps.max_reference_images:
                    refs = refs[: self.caps.max_reference_images - 1]
                kf = req.pop("image")
                req["reference_images"] = refs + [{"label": "approved keyframe (opening frame of this shot)",
                                                   "path": kf, "sha": hash_file(Path(kf))}]
            else:
                notes.append(f"reference images dropped: {self.name} cannot mix them with a first frame; the approved "
                             "keyframe already carries the asset sheets (providers.yaml video_mode: references to switch)")
                req.pop("reference_images")
        return req, notes

    def _prompt_with_refs(self, request) -> str:
        refs = request.get("reference_images") or []
        return (reference_header(refs) + "\n" + request["prompt"]) if refs else request["prompt"]

    def poll(self, remote_id):
        d, problem = self._get_json(self._poll_url(remote_id))
        if problem:
            return problem
        state, url, err = self._parse_poll(d)
        if state == "completed":
            if not url:
                return PollResult("failed", error=f"{self.name}: task succeeded without a video URL")
            self._results[remote_id] = d
        return PollResult(state, error=err)

    def fetch(self, remote_id, workdir):
        d = self._results.get(remote_id)
        if d is None:
            d, problem = self._get_json(self._poll_url(remote_id))
            if problem:
                raise ProviderError(f"{self.name}: fetch could not read the task ({problem.error})", retryable=True)
        state, url, err = self._parse_poll(d)
        if state != "completed" or not url:
            raise ProviderError(f"{self.name}: task {remote_id} is {state} ({err or 'no video URL'})")
        out = self._download(url, Path(workdir) / f"{self.name}_{remote_id[-24:].replace('/', '_')}.mp4")
        info = avtools.probe(out)
        usage = {"seconds": info["duration"], **self._usage(d)}
        return Output(out, "video/mp4", usage=usage, meta={**info, "provider_task": remote_id})

    def _usage(self, d) -> Dict[str, Any]:
        return {}

    def actual_cost(self, request, usage) -> Tuple[Optional[float], str]:
        p = self.price.get("usd_per_1m_tokens")
        if p is not None and usage.get("total_tokens"):
            return round(usage["total_tokens"] / 1e6 * float(p), 6), "provider usage tokens x configured token price"
        return super().actual_cost(request, usage)

    def _poll_url(self, remote_id: str) -> str:
        raise NotImplementedError

    def _parse_poll(self, d) -> Tuple[str, Optional[str], Optional[str]]:
        raise NotImplementedError


def _int_durations(lo: int, hi: int) -> List[float]:
    return [float(x) for x in range(lo, hi + 1)]


# ── Seedance 2.0 (Volcengine Ark / BytePlus ModelArk) ──────────────────────

class SeedanceVideoProvider(_AsyncVideo):
    name = "seedance"
    DEFAULT_BASE = "https://ark.ap-southeast.bytepluses.com/api/v3"     # BytePlus (international)
    KEY_ENVS = ("ARK_API_KEY",)
    GATEWAY_PATH = "/volcengine"
    mixes_first_frame_and_references = False
    caps = ProviderCapabilities(
        kind="video", accepted_fields=("prompt", "duration", "size", "seed", "image", "reference_images"),
        sizes=["1280x720", "720x1280", "864x480", "480x864", "960x960"], durations=_int_durations(4, 15),
        max_reference_images=9, first_frame_image=True, generates_audio=True, lip_sync=False,
        negative_prompt=False, seed=True, idempotency=False, cancellation=True, usage_reporting=True,
        async_polling=True,
        constraints=["cancel works only while queued (a running task finishes and bills)",
                     "4-15 s, whole seconds", "fast/mini: 480p or 720p; standard 2.0 also 1080p",
                     "first frame OR up to 9 reference images (documented as mutually exclusive)",
                     "generate_audio defaults to true at the API; we send false unless providers.yaml says otherwise",
                     "result URLs expire after 24 h", "billed by output tokens (usage.total_tokens)"],
        docs="https://ai.byteplus.com/ark/region:ap-southeast-1/docs/create-video-generation-task-api.md",
    )

    def __init__(self, model, cfg=None, price=None, transport=None, api_key=None, base_url=None):
        super().__init__(model, cfg, price, transport, api_key, base_url)
        if not any(t in (model or "") for t in ("fast", "mini")):
            from dataclasses import replace
            self.caps = replace(self.caps, sizes=list(self.caps.sizes) + ["1920x1080", "1080x1920"])

    def _base(self) -> str:
        return join_url(self.base_url, "/api/v3", "")

    def submit(self, request, idempotency_key, workdir):
        self.validate(request)
        content: List[Dict[str, Any]] = [{"type": "text", "text": self._prompt_with_refs(request)}]
        if request.get("image"):
            content.append({"type": "image_url", "image_url": {"url": data_uri(request["image"])}, "role": "first_frame"})
        for r in request.get("reference_images") or []:
            content.append({"type": "image_url", "image_url": {"url": data_uri(r["path"])}, "role": "reference_image"})
        w, h = _wh(request.get("size", "720x1280"))
        body: Dict[str, Any] = {
            "model": self.model, "content": content,
            "ratio": "adaptive" if request.get("image") else _ratio(request.get("size")),
            "resolution": "1080p" if min(w, h) >= 1080 else ("720p" if min(w, h) >= 720 else "480p"),
            "duration": int(request.get("duration", 5)),
            "generate_audio": bool(self.cfg.get("generate_audio", False)),
            "watermark": False,
        }
        if request.get("seed") is not None:
            body["seed"] = int(request["seed"])
        d = self._post(self._base() + "/contents/generations/tasks", body)
        if not d.get("id"):
            raise ProviderError(f"seedance: no task id in response: {str(d)[:200]}")
        return SubmitResult(str(d["id"]))

    def _poll_url(self, remote_id):
        return self._base() + f"/contents/generations/tasks/{remote_id}"

    def cancel(self, remote_id):
        """DELETE cancels a queued task (a running one cannot be stopped; it finishes and is billed)."""
        return self._delete(self._poll_url(remote_id))

    def _parse_poll(self, d):
        st = d.get("status")
        url = (d.get("content") or {}).get("video_url")
        if st == "succeeded":
            return "completed", url, None
        if st in ("failed", "cancelled", "expired"):
            e = d.get("error") or {}
            return "failed", None, (f"[{e.get('code')}] {e.get('message')}" if isinstance(e, dict) else str(e)) or st
        return ("queued" if st == "queued" else "running"), None, None

    def _usage(self, d):
        u = d.get("usage") or {}
        return {k: u[k] for k in ("completion_tokens", "total_tokens") if k in u}


# ── MiniMax H3 ─────────────────────────────────────────────────────────────

class MiniMaxVideoProvider(_AsyncVideo):
    name = "minimax-video"
    DEFAULT_BASE = "https://api.minimax.io"
    KEY_ENVS = ("MINIMAX_API_KEY",)
    GATEWAY_PATH = "/minimax"
    mixes_first_frame_and_references = False
    caps = ProviderCapabilities(
        kind="video", accepted_fields=("prompt", "duration", "size", "image", "reference_images"),
        sizes=["1366x768", "768x1366", "768x768", "2560x1440", "1440x2560"], durations=_int_durations(4, 15),
        max_reference_images=9, first_frame_image=True, generates_audio=True, lip_sync=False,
        negative_prompt=False, seed=False, idempotency=False, cancellation=True, usage_reporting=False,
        async_polling=True,
        constraints=["cancel works only while queued", "prompt <= 7000 chars", "MiniMax-H3: 768P or 2K, 4-15 s",
                     "first frame OR reference images (documented as mutually exclusive)",
                     "no audio switch in the API (any clip audio is dropped by the studio mix by default)",
                     "with a first frame the aspect follows the image (ratio omitted)"],
        docs="https://platform.minimax.io/docs/api-reference/video-generation-v2-create.md",
    )

    def validate(self, request):
        super().validate(request)
        if len(self._prompt_with_refs(request)) > 7000:
            raise UnsupportedParameter("minimax-video: prompt longer than 7000 characters")

    def submit(self, request, idempotency_key, workdir):
        self.validate(request)
        content: List[Dict[str, Any]] = [{"type": "text", "text": self._prompt_with_refs(request)}]
        if request.get("image"):
            content.append({"type": "image_url", "image_url": {"url": data_uri(request["image"])}, "role": "first_frame"})
        for r in request.get("reference_images") or []:
            content.append({"type": "image_url", "image_url": {"url": data_uri(r["path"])}, "role": "reference_image"})
        w, h = _wh(request.get("size", "768x1366"))
        body: Dict[str, Any] = {"model": self.model, "content": content, "duration": int(request.get("duration", 6)),
                                "resolution": "2K" if min(w, h) >= 1080 else "768P"}
        if not request.get("image"):
            body["ratio"] = _ratio(request.get("size"))
        d = self._post(join_url(self.base_url, "", "/v2/video_generation"), body)
        base = d.get("base_resp") or {}
        if base.get("status_code") not in (None, 0):          # MiniMax reports some refusals with HTTP 200
            if base["status_code"] in (1002, 1039):
                raise RateLimited(f"minimax-video: {base.get('status_msg')}")
            raise ProviderError(f"minimax-video: [{base['status_code']}] {base.get('status_msg')}")
        tid = d.get("task_id") or (d.get("task") or {}).get("task_id")
        if not tid:
            raise ProviderError(f"minimax-video: no task_id in response: {_summ(d)}")
        return SubmitResult(str(tid))

    def _poll_url(self, remote_id):
        return join_url(self.base_url, "", f"/v2/query/video_generation/{remote_id}")

    def cancel(self, remote_id):
        return self._delete(join_url(self.base_url, "", f"/v2/video_generation/{remote_id}"))

    def _parse_poll(self, d):
        t = d.get("task") if isinstance(d.get("task"), dict) else d
        st = t.get("status")
        if st == "succeeded":
            return "completed", (t.get("content") or {}).get("url"), None
        if st in ("failed", "cancelled"):
            e = t.get("error") or {}
            return "failed", None, (f"[{e.get('code')}] {e.get('message')}" if isinstance(e, dict) else str(e)) or st
        return ("queued" if st in ("queued", "pending") else "running"), None, None


def _summ(d) -> str:
    return str(d)[:200]


# ── Wan 3.0 (Alibaba Cloud Model Studio / DashScope) ───────────────────────

class WanVideoProvider(_AsyncVideo):
    name = "wan-video"
    DEFAULT_BASE = "https://dashscope-intl.aliyuncs.com"    # docs show https://{WorkspaceId}.<region>.maas.aliyuncs.com:
    KEY_ENVS = ("DASHSCOPE_API_KEY",)                         # set DASHSCOPE_BASE_URL to your workspace host
    GATEWAY_PATH = "/qwen"
    mixes_first_frame_and_references = False
    caps = ProviderCapabilities(
        kind="video", accepted_fields=("prompt", "duration", "size", "seed", "image", "reference_images"),
        sizes=["1920x1080", "1080x1920", "1280x720", "720x1280", "854x480", "480x854"], durations=_int_durations(2, 30),
        max_reference_images=10, first_frame_image=True, generates_audio=True, lip_sync=False,
        negative_prompt=False, seed=True, idempotency=False, cancellation=False, usage_reporting=True,
        async_polling=True,
        constraints=["2-30 s, whole seconds", "480P / 720P / 1080P",
                     "first frame OR up to 10 reference images (not both)", "prompt_extend off by default (our prompts "
                     "are already resolved)", "task results are queryable for 24 h"],
        docs="https://help.aliyun.com/en/model-studio/wan3-video-generation-api-reference",
    )

    def __init__(self, model, cfg=None, price=None, transport=None, api_key=None, base_url=None):
        cfg = dict(cfg or {})
        cfg.setdefault("base_url_env", "DASHSCOPE_BASE_URL")
        super().__init__(model, cfg, price, transport, api_key, base_url)

    def _headers(self):
        return {**super()._headers(), "X-DashScope-Async": "enable"}

    def submit(self, request, idempotency_key, workdir):
        self.validate(request)
        media: List[Dict[str, str]] = []
        if request.get("image"):
            media.append({"type": "first_frame", "url": data_uri(request["image"])})
        for r in request.get("reference_images") or []:
            media.append({"type": "reference_image", "url": data_uri(r["path"])})
        w, h = _wh(request.get("size", "720x1280"))
        params: Dict[str, Any] = {
            "resolution": "1080P" if min(w, h) >= 1080 else ("720P" if min(w, h) >= 720 else "480P"),
            "ratio": "adaptive" if request.get("image") else _ratio(request.get("size")),
            "duration": int(request.get("duration", 5)),
            "audio": bool(self.cfg.get("generate_audio", False)),
            "prompt_extend": bool(self.cfg.get("prompt_extend", False)),
            "watermark": False,
        }
        if request.get("seed") is not None:
            params["seed"] = int(request["seed"]) % 2147483647
        body = {"model": self.model, "input": {"prompt": self._prompt_with_refs(request)[:20000], "media": media},
                "parameters": params}
        if not media:
            body["input"].pop("media")
        d = self._post(join_url(self.base_url, "/api/v1", "/services/aigc/video-generation/video-synthesis"), body)
        tid = (d.get("output") or {}).get("task_id")
        if not tid:
            raise ProviderError(f"wan-video: no output.task_id: {_err_text_json(d)}")
        return SubmitResult(str(tid))

    def _poll_url(self, remote_id):
        return join_url(self.base_url, "/api/v1", f"/tasks/{remote_id}")

    def _parse_poll(self, d):
        o = d.get("output") or {}
        st = o.get("task_status")
        if st == "SUCCEEDED":
            return "completed", o.get("video_url"), None
        if st in ("FAILED", "CANCELED", "UNKNOWN"):
            return "failed", None, f"[{o.get('code', st)}] {o.get('message', 'task ' + st.lower())}"
        return ("queued" if st == "PENDING" else "running"), None, None

    def _usage(self, d):
        u = d.get("usage") or {}
        sec = u.get("output_video_duration", u.get("duration"))
        return {"billed_seconds": sec} if sec is not None else {}


def _err_text_json(d) -> str:
    return f"[{d.get('code', '')}] {d.get('message', '')}"[:300] if isinstance(d, dict) else str(d)[:300]


# ── images ─────────────────────────────────────────────────────────────────

class SeedreamImageProvider(_HttpProvider):
    name, kind = "seedream-image", "image"
    DEFAULT_BASE = "https://ark.ap-southeast.bytepluses.com/api/v3"
    KEY_ENVS = ("ARK_API_KEY",)
    GATEWAY_PATH = "/volcengine"
    caps = ProviderCapabilities(
        kind="image", accepted_fields=("prompt", "size", "seed", "reference_images"),
        sizes=["1440x2560", "2560x1440", "2048x2048", "1728x2304", "2304x1728"], max_reference_images=10,
        negative_prompt=False, seed=True, idempotency=False, usage_reporting=True, async_polling=False,
        constraints=["synchronous", "up to 10 reference images (base64 data URIs)", "returns base64 (b64_json)"],
        docs="https://ai.byteplus.com/ark/region:ap-southeast-1/docs/image-generation-api.md",
    )

    def submit(self, request, idempotency_key, workdir):
        self.validate(request)
        body: Dict[str, Any] = {"model": self.model, "prompt": self._prompt(request),
                                "size": request.get("size", "1440x2560"), "response_format": "b64_json",
                                "watermark": False}
        refs = request.get("reference_images") or []
        if refs:
            body["image"] = [data_uri(r["path"]) for r in refs]
        if request.get("seed") is not None:
            body["seed"] = int(request["seed"])
        d = self._post(join_url(self.base_url, "/api/v3", "/images/generations"), body, timeout=300)
        item = (d.get("data") or [{}])[0]
        workdir.mkdir(parents=True, exist_ok=True)
        out = Path(workdir) / f"seedream_{idempotency_key[:12]}.png"
        if item.get("b64_json"):
            out.write_bytes(base64.b64decode(item["b64_json"]))
        elif item.get("url"):
            self._download(item["url"], out)
        else:
            raise ProviderError(f"seedream-image: no image in response: {_err_text_json(d)}")
        return SubmitResult(f"sync_{idempotency_key[:12]}", Output(out, "image/png", usage=d.get("usage") or {}))

    @staticmethod
    def _prompt(request) -> str:
        refs = request.get("reference_images") or []
        return (reference_header(refs) + "\n" + request["prompt"]) if refs else request["prompt"]


class GeminiImageProvider(_HttpProvider):
    name, kind = "gemini-image", "image"
    DEFAULT_BASE = "https://generativelanguage.googleapis.com"
    KEY_ENVS = ("GEMINI_API_KEY",)
    GATEWAY_PATH = ""
    caps = ProviderCapabilities(
        kind="image", accepted_fields=("prompt", "size", "reference_images"),
        sizes=["768x1344", "1344x768", "1024x1024", "864x1184", "1184x864"], max_reference_images=8,
        negative_prompt=False, seed=False, idempotency=False, usage_reporting=True, async_polling=False,
        constraints=["synchronous", "size is sent as an aspect ratio; the model picks the pixel size",
                     "reference images inline (base64)"],
        docs="https://ai.google.dev/gemini-api/docs/image-generation",
    )

    def _headers(self):
        return {"x-goog-api-key": self.api_key}

    def submit(self, request, idempotency_key, workdir):
        self.validate(request)
        parts: List[Dict[str, Any]] = []
        refs = request.get("reference_images") or []
        for r in refs:
            uri = data_uri(r["path"])
            parts.append({"inline_data": {"mime_type": uri[5:uri.index(";")], "data": uri.split(",", 1)[1]}})
        parts.append({"text": (reference_header(refs) + "\n" if refs else "") + request["prompt"]})
        body = {"contents": [{"parts": parts}],
                "generationConfig": {"responseModalities": ["IMAGE"],
                                     "imageConfig": {"aspectRatio": _ratio(request.get("size")) or "9:16"}}}
        model = self.model if self.model.startswith("models/") else f"models/{self.model}"
        d = self._post(join_url(self.base_url, "/v1beta", f"/{model}:generateContent"), body, timeout=300)
        cands = d.get("candidates") or []
        for part in ((cands[0].get("content") or {}).get("parts") or []) if cands else []:
            blob = part.get("inlineData") or part.get("inline_data")
            if blob and blob.get("data"):
                workdir.mkdir(parents=True, exist_ok=True)
                out = Path(workdir) / f"gemini_{idempotency_key[:12]}.png"
                out.write_bytes(base64.b64decode(blob["data"]))
                return SubmitResult(f"sync_{idempotency_key[:12]}",
                                    Output(out, blob.get("mimeType") or blob.get("mime_type") or "image/png",
                                           usage=d.get("usageMetadata") or {}))
        reason = (cands[0].get("finishReason") if cands else None) or (d.get("promptFeedback") or {}).get("blockReason")
        raise ProviderError(f"gemini-image: no image returned (finish/block reason: {reason})")


# ── TTS: MiniMax T2A v2 ────────────────────────────────────────────────────

class MiniMaxTTSProvider(_HttpProvider):
    name, kind = "minimax-tts", "tts"
    DEFAULT_BASE = "https://api.minimax.io"
    KEY_ENVS = ("MINIMAX_API_KEY",)
    GATEWAY_PATH = "/minimax"
    caps = ProviderCapabilities(
        kind="tts", accepted_fields=("text", "voice", "speed", "format", "language"),
        max_text_chars=10000, idempotency=False, usage_reporting=True,
        constraints=["voice = MiniMax voice_id (providers.yaml voice_map translates planned voice names)",
                     "language_boost from the project language",
                     "free-text voice instructions are not supported and are dropped (recorded as an adaptation)"],
        docs="https://platform.minimax.io/docs/api-reference/speech-t2a-http.md",
    )
    LANG = {"vi": "Vietnamese", "en": "English", "zh": "Chinese", "ja": "Japanese", "ko": "Korean",
            "th": "Thai", "id": "Indonesian", "fr": "French", "es": "Spanish"}

    def adapt(self, request):
        req, notes = super().adapt(request)
        vmap = self.cfg.get("voice_map") or {}
        if req.get("voice") in vmap:
            notes.append(f"voice {req['voice']} -> {vmap[req['voice']]} (voice_map)")
            req["voice"] = vmap[req["voice"]]
        return req, notes

    def validate(self, request):
        super().validate(request)
        allowed = {v for v in (self.cfg.get("voice_map") or {}).values() if v} | set(self.cfg.get("voices") or [])
        if allowed and request.get("voice") not in allowed:
            raise UnsupportedParameter(f"minimax-tts: voice {request.get('voice')!r} has no MiniMax voice_id "
                                       "(set it in providers.yaml tts.voice_map)")

    def submit(self, request, idempotency_key, workdir):
        self.validate(request)
        body: Dict[str, Any] = {
            "model": self.model, "text": request["text"], "stream": False,
            "voice_setting": {"voice_id": request["voice"], "speed": float(request.get("speed") or 1.0),
                              "vol": 1.0, "pitch": 0},
            "audio_setting": {"sample_rate": 32000, "format": "wav", "channel": 1},
            "output_format": "hex",
        }
        lang = self.LANG.get((request.get("language") or "").split("-")[0])
        if lang:
            body["language_boost"] = lang
        d = self._post(join_url(self.base_url, "", "/v1/t2a_v2"), body, timeout=180)
        base = d.get("base_resp") or {}
        if base.get("status_code") not in (None, 0):
            code = base.get("status_code")
            if code in (1002, 1039):               # documented rate-limit codes
                raise RateLimited(f"minimax-tts: {base.get('status_msg')}")
            raise ProviderError(f"minimax-tts: [{code}] {base.get('status_msg')}")
        audio = (d.get("data") or {}).get("audio")
        if not audio:
            raise ProviderError("minimax-tts: response has no data.audio")
        workdir.mkdir(parents=True, exist_ok=True)
        out = Path(workdir) / f"mmtts_{idempotency_key[:12]}.wav"
        out.write_bytes(bytes.fromhex(audio))
        extra = d.get("extra_info") or {}
        return SubmitResult(f"sync_{idempotency_key[:12]}",
                            Output(out, "audio/wav", usage={"characters": extra.get("usage_characters",
                                                                                      len(request["text"]))}))
