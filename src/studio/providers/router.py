#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Copyright (c) 2026 Sixun Dong
# Author: Sixun Dong
# Licensed under the Apache License, Version 2.0
"""
ShotRouterVideoProvider — one "video" provider that sends each shot to the backend its FilmDSL calls for:

  dialogue      a speaker on screen  -> a lip-sync model (image + the shot's dialogue track -> talking clip)
  action        characters, no line  -> an image-to-video model (prompt from the staging layer)
  establishing  no characters        -> a camera move over the keyframe (motion-still, $0)

The engine tags each clip request with `shot_kind` (from the blueprint) and, for providers that lip-sync, with
`audio` (a shot-length track holding the dialogue at the exact offset the export mixes it). The chosen route is
recorded in the request (`route`) and every adaptation the route makes is recorded too, so a clip says which
backend made it and why. GPU routes are optional: point any kind at motion-still and nothing needs a GPU.
"""

from typing import Any, Dict, Tuple

from .base import NotConfigured, Provider, ProviderCapabilities

KINDS = ("dialogue", "action", "establishing")


class ShotRouterVideoProvider(Provider):
    name, kind = "shot-router", "video"

    def __init__(self, routes: Dict[str, Provider], by_kind: Dict[str, str], default: str):
        super().__init__({})
        if default not in routes:
            raise NotConfigured(f"shot-router: default route {default!r} is not defined")
        for k, r in by_kind.items():
            if r not in routes:
                raise NotConfigured(f"shot-router: {k} -> {r!r} is not a defined route")
        self.routes, self.by_kind, self.default = routes, dict(by_kind), default
        self.model = "+".join(f"{n}={p.name}:{p.model}" for n, p in sorted(routes.items()))
        self.api_key = "local" if all(getattr(p, "api_key", None) for p in routes.values()) else ""
        fields = {"route", "shot_kind"}
        for p in routes.values():
            fields |= set(p.caps.accepted_fields)
        self.caps = ProviderCapabilities(
            kind="video", accepted_fields=tuple(sorted(fields)),
            min_duration=min(p.caps.min_duration for p in routes.values()),
            max_duration=max(p.caps.max_duration for p in routes.values()),
            first_frame_image=True, lip_sync=any(p.caps.lip_sync for p in routes.values()),
            idempotency=False, cancellation=any(p.caps.cancellation for p in routes.values()), async_polling=True,
            constraints=[f"{k} -> {self.by_kind.get(k, default)}" for k in KINDS],
            docs="docs/studio/MOTION_LIPSYNC.md",
        )

    def route_for(self, request: Dict[str, Any]) -> str:
        if request.get("route") in self.routes:
            return request["route"]
        return self.by_kind.get(request.get("shot_kind") or "", self.default)

    def skips_clip_review(self, request: Dict[str, Any]) -> bool:
        return getattr(self.routes[self.route_for(request)], "review_clips", True) is False

    def adapt(self, request) -> Tuple[Dict[str, Any], list]:
        name = self.route_for(request)
        inner = self.routes[name]
        req, notes = inner.adapt({k: v for k, v in request.items() if k not in ("route", "shot_kind")})
        notes.insert(0, f"route {request.get('shot_kind') or 'unspecified'} -> {name} ({inner.name}:{inner.model})")
        req["route"] = name
        if request.get("shot_kind"):
            req["shot_kind"] = request["shot_kind"]
        return req, notes

    def validate(self, request):
        self.routes[self.route_for(request)].validate(self._inner(request))

    @staticmethod
    def _inner(request):
        return {k: v for k, v in request.items() if k not in ("route", "shot_kind")}

    def estimate_cost(self, request):
        return self.routes[self.route_for(request)].estimate_cost(self._inner(request))

    def actual_cost(self, request, usage):
        return self.routes[self.route_for(request)].actual_cost(self._inner(request), usage)

    def submit(self, request, idempotency_key, workdir):
        name = self.route_for(request)
        sr = self.routes[name].submit(self._inner(request), idempotency_key, workdir)
        sr.remote_id = f"{name}|{sr.remote_id}"
        if sr.output is not None:
            sr.output.meta = {**(sr.output.meta or {}), "route": name}
        return sr

    def _split(self, remote_id: str):
        name, _, rid = remote_id.partition("|")
        if name not in self.routes:
            raise NotConfigured(f"shot-router: unknown route in remote id {remote_id!r}")
        return self.routes[name], rid, name

    def poll(self, remote_id):
        p, rid, _ = self._split(remote_id)
        return p.poll(rid)

    def fetch(self, remote_id, workdir):
        p, rid, name = self._split(remote_id)
        out = p.fetch(rid, workdir)
        out.meta = {**(out.meta or {}), "route": name}
        return out

    def cancel(self, remote_id):
        p, rid, _ = self._split(remote_id)
        return p.cancel(rid)

    def describe(self):
        d = super().describe()
        d["routes"] = {n: p.describe() for n, p in self.routes.items()}
        return d
