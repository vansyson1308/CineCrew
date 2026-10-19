#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Copyright (c) 2026 Sixun Dong
# Author: Sixun Dong
# Licensed under the Apache License, Version 2.0
"""
Provider contract for the studio's execution layer.

Every backend declares what it supports (ProviderCapabilities). Requests are plain dicts with
standard keys; `adapt()` turns a planned request into one the backend accepts and records every
change it made (duration snapped, negative prompt dropped, ...) in `adaptations`, so nothing is
silently assumed. `validate()` is strict: an unsupported, non-empty field raises
UnsupportedParameter instead of being forwarded and ignored (or rejected) by the server.

Lifecycle: submit() -> remote id (+ output immediately for synchronous backends) ->
poll() -> fetch(). Synchronous backends return their output from submit().
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ── errors ─────────────────────────────────────────────────────────────────

class ProviderError(Exception):
    """A request the provider definitively did not complete. `retryable` = transport-level retry may help."""

    def __init__(self, message: str, retryable: bool = False, code: Optional[str] = None):
        super().__init__(message)
        self.retryable, self.code = retryable, code


class RateLimited(ProviderError):
    def __init__(self, message: str = "rate limited", retry_after: Optional[float] = None):
        super().__init__(message, retryable=True, code="rate_limited")
        self.retry_after = retry_after


class UnsupportedParameter(ProviderError):
    def __init__(self, message: str):
        super().__init__(message, retryable=False, code="unsupported_parameter")


class AmbiguousSubmission(ProviderError):
    """The request may or may not have been accepted (connection lost after sending)."""

    def __init__(self, message: str):
        super().__init__(message, retryable=False, code="ambiguous_submission")


class NotConfigured(ProviderError):
    def __init__(self, message: str):
        super().__init__(message, retryable=False, code="not_configured")


# ── capability declaration ─────────────────────────────────────────────────

@dataclass
class ProviderCapabilities:
    kind: str                                   # image | video | tts | judge
    accepted_fields: Tuple[str, ...]            # request keys this backend understands
    sizes: Optional[List[str]] = None           # None = any WxH
    durations: Optional[List[float]] = None     # discrete allowed seconds (video); None = range
    min_duration: float = 0.0
    max_duration: float = 0.0
    max_reference_images: int = 0
    first_frame_image: bool = False             # video: conditions on a start frame (I2V)
    generates_audio: bool = False               # video: returns an audio track
    lip_sync: bool = False                      # audio-driven lip sync to a supplied voice track
    negative_prompt: bool = False
    seed: bool = False
    idempotency: bool = False                   # resubmitting with the same key cannot double-bill
    cancellation: bool = False
    usage_reporting: bool = False               # returns usage the cost can be computed from
    async_polling: bool = False
    max_text_chars: Optional[int] = None        # tts
    voices: Optional[List[str]] = None          # tts
    constraints: List[str] = field(default_factory=list)   # human-readable rules (e.g. refs need 8 s)
    docs: Optional[str] = None                  # official documentation the adapter follows

    def to_dict(self) -> Dict[str, Any]:
        from dataclasses import asdict
        d = asdict(self)
        d["accepted_fields"] = list(self.accepted_fields)
        return d


@dataclass
class Output:
    path: Path
    mime: str
    usage: Dict[str, Any] = field(default_factory=dict)
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SubmitResult:
    remote_id: str
    output: Optional[Output] = None             # synchronous backends deliver here


@dataclass
class PollResult:
    state: str                                  # queued | running | completed | failed | unknown
    error: Optional[str] = None
    progress: Optional[float] = None


class Provider:
    """Base class. Subclasses set name/kind/model/caps and implement submit/poll/fetch."""

    name: str = "base"
    kind: str = "image"
    model: Optional[str] = None
    is_fixture: bool = False
    caps: ProviderCapabilities

    def __init__(self, price: Optional[Dict[str, Any]] = None):
        # price: {"unit": "image"|"second"|"1k_chars"|"call"|"1m_tokens", "usd": float | None,
        #         "max_usd_per_call": float | None}
        self.price = price or {}

    # ── request shaping ───────────────────────────────────────────────────
    def adapt(self, request: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
        """Return (request the backend accepts, list of explicit adaptations). Never silent."""
        req, notes = dict(request), []
        c = self.caps
        for key in list(req):
            if req[key] in (None, "", [], {}):
                req.pop(key)
                continue
            if key not in c.accepted_fields:
                notes.append(f"dropped '{key}': not supported by {self.name}")
                req.pop(key)
        if c.kind == "video" and "duration" in req:
            want = float(req["duration"])
            if c.durations:
                fit = [d for d in c.durations if d >= want - 1e-6]
                got = min(fit) if fit else max(c.durations)
            else:
                got = min(max(want, c.min_duration or want), c.max_duration or want)
            if abs(got - want) > 1e-6:
                notes.append(f"duration {want:g}s -> {got:g}s (supported: {c.durations or [c.min_duration, c.max_duration]}); "
                             "assembly trims/holds to the planned slot")
            req["duration"] = got
        if c.sizes and req.get("size") and req["size"] not in c.sizes:
            got = _closest_size(req["size"], c.sizes)
            notes.append(f"size {req['size']} -> {got} (supported: {c.sizes}); export records the conversion")
            req["size"] = got
        refs = req.get("reference_images") or []
        if refs and len(refs) > c.max_reference_images:
            notes.append(f"reference_images {len(refs)} -> {c.max_reference_images} (backend limit)")
            req["reference_images"] = refs[: c.max_reference_images]
            if not req["reference_images"]:
                req.pop("reference_images")
        return req, notes

    def validate(self, request: Dict[str, Any]) -> None:
        unknown = [k for k, v in request.items() if v not in (None, "", [], {}) and k not in self.caps.accepted_fields]
        if unknown:
            raise UnsupportedParameter(f"{self.name} does not accept {unknown}")
        c = self.caps
        if c.sizes and request.get("size") and request["size"] not in c.sizes:
            raise UnsupportedParameter(f"{self.name}: size {request['size']} not in {c.sizes}")
        if c.kind == "video" and c.durations and "duration" in request and float(request["duration"]) not in c.durations:
            raise UnsupportedParameter(f"{self.name}: duration {request['duration']} not in {c.durations}")
        if c.max_text_chars and len(request.get("text", "")) > c.max_text_chars:
            raise UnsupportedParameter(f"{self.name}: text longer than {c.max_text_chars} chars")
        if len(request.get("reference_images") or []) > c.max_reference_images:
            raise UnsupportedParameter(f"{self.name}: at most {c.max_reference_images} reference images")

    # ── cost ──────────────────────────────────────────────────────────────
    def estimate_cost(self, request: Dict[str, Any]) -> Optional[float]:
        """Conservative USD estimate for one call, or None when the price is unknown."""
        if self.is_fixture:
            return float(self.price.get("usd", 0.0) or 0.0)
        cap = self.price.get("max_usd_per_call")
        usd, unit = self.price.get("usd"), self.price.get("unit")
        if usd is None:
            return float(cap) if cap is not None else None
        qty = {
            "second": float(request.get("duration", 0) or 0),
            "1k_chars": len(request.get("text", "")) / 1000.0,
        }.get(unit, 1.0)
        est = float(usd) * qty
        return max(est, float(cap)) if cap is not None else est

    def actual_cost(self, request: Dict[str, Any], usage: Dict[str, Any]) -> Tuple[Optional[float], str]:
        """(usd, basis). Default: the provider reports no usage -> the estimate is the recorded cost."""
        est = self.estimate_cost(request)
        return est, "estimate (provider reports no usage)"

    # ── lifecycle ─────────────────────────────────────────────────────────
    def submit(self, request: Dict[str, Any], idempotency_key: str, workdir: Path) -> SubmitResult:
        raise NotImplementedError

    def poll(self, remote_id: str) -> PollResult:
        raise NotImplementedError

    def fetch(self, remote_id: str, workdir: Path) -> Output:
        raise NotImplementedError

    def cancel(self, remote_id: str) -> bool:
        return False

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "model": self.model, "fixture": self.is_fixture,
                "price": self.price, "capabilities": self.caps.to_dict()}


def _closest_size(size: str, allowed: List[str]) -> str:
    def wh(s):
        w, h = s.lower().split("x")
        return int(w), int(h)
    w, h = wh(size)
    ar = w / h
    # same aspect (to 1%), then the nearest pixel count: 720x1280 must map to 768x1366, not to a 2K tier
    return min(allowed, key=lambda s: (round(abs(wh(s)[0] / wh(s)[1] - ar) / ar, 2), abs(wh(s)[0] * wh(s)[1] - w * h)))
