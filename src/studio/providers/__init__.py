#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Copyright (c) 2026 Sixun Dong
# Author: Sixun Dong
# Licensed under the Apache License, Version 2.0
"""Provider registry: which backend serves each kind (image / video / tts / judge) for a project mode."""

import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from .base import (  # noqa: F401
    AmbiguousSubmission, NotConfigured, Output, PollResult, Provider, ProviderCapabilities, ProviderError, RateLimited,
    SubmitResult, UnsupportedParameter,
)
from .fixture import FixtureImageProvider, FixtureJudgeProvider, FixtureTTSProvider, FixtureVideoProvider

_ROOT = Path(__file__).resolve().parents[3]
PROVIDERS_YAML = _ROOT / "configs" / "studio" / "providers.yaml"


def load_provider_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """providers.yaml, or the file named by STUDIO_PROVIDERS (e.g. configs/studio/providers.huobao.yaml)."""
    path = path or (Path(os.environ["STUDIO_PROVIDERS"]) if os.getenv("STUDIO_PROVIDERS") else PROVIDERS_YAML)
    if not path.is_absolute() and not path.exists():
        path = _ROOT / path
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# providers.yaml `provider:` name -> class in multiref.py (the huobao-drama backend line-up)
MULTIREF = {"seedance": "SeedanceVideoProvider", "minimax-video": "MiniMaxVideoProvider",
            "wan-video": "WanVideoProvider", "seedream-image": "SeedreamImageProvider",
            "gemini-image": "GeminiImageProvider", "minimax-tts": "MiniMaxTTSProvider"}


def _huobao_base() -> str:
    return (_env("HUOBAO_BASE_URL") or "https://api.firemux.com").rstrip("/")


def _env(*names: str) -> str:
    for n in names:
        if os.getenv(n):
            return os.environ[n]
    return ""


class Registry:
    """Lazily-built provider set for one mode. Fixture providers keep their remote state under state_dir."""

    def __init__(self, mode: str, state_dir: Path, config: Optional[Dict[str, Any]] = None,
                 overrides: Optional[Dict[str, Provider]] = None):
        self.mode, self.state_dir = mode, Path(state_dir)
        self.config = config or load_provider_config()
        self._cache: Dict[str, Provider] = dict(overrides or {})

    def get(self, kind: str) -> Provider:
        if kind not in self._cache:
            self._cache[kind] = self._build(kind)
        return self._cache[kind]

    def _build(self, kind: str) -> Provider:
        if self.mode != "live":
            cfg = (self.config.get("offline_fixture") or {}).get(kind) or {}
            price = cfg.get("price")
            if kind == "image":
                return FixtureImageProvider(price)
            if kind == "video":
                return FixtureVideoProvider(self.state_dir / "fixture_video", float(cfg.get("latency_seconds", 0.4)), price)
            if kind == "tts":
                return FixtureTTSProvider(price)
            if kind == "judge":
                return FixtureJudgeProvider(price)
            raise KeyError(kind)
        from . import live, multiref
        cfg = (self.config.get("live") or {}).get(kind) or {}
        price, name = cfg.get("price"), cfg.get("provider")
        llm_key = _env("LLM_API_KEY", "OPENAI_API_KEY")
        if name in MULTIREF:
            cls = getattr(multiref, MULTIREF[name])
            if cls.kind != kind:
                raise NotConfigured(f"provider {name!r} serves {cls.kind}, not {kind}")
            return cls(cfg.get("model") or "", cfg, price)
        if kind == "image" and name == "openai-image":
            key, base = _env("T2I_API_KEY") or llm_key, _env("T2I_BASE_URL") or None
            if not key and _env("HUOBAO_API_KEY") and cfg.get("gateway", True):
                key, base = _env("HUOBAO_API_KEY"), _huobao_base() + "/v1"
            return live.OpenAIImageProvider(cfg.get("model", "gpt-image-1"), key, base, price)
        if kind == "video" and name == "veo":
            return live.VeoVideoProvider(cfg.get("model", "veo-3.1-generate-preview"),
                                         _env("VEO_API_KEY", "GEMINI_API_KEY"), price)
        if kind == "video" and name == "openai-compatible-video":
            return live.OpenAICompatVideoProvider(cfg.get("model", ""), _env(cfg.get("base_url_env", "VIDEO_BASE_URL")),
                                                  _env("VIDEO_API_KEY") or "EMPTY", cfg.get("capabilities"), price)
        if kind == "tts" and name == "openai-tts":
            return live.OpenAITTSProvider(cfg.get("model", "gpt-4o-mini-tts"), _env("TTS_API_KEY") or llm_key,
                                          _env("TTS_BASE_URL") or None, price)
        if kind == "judge" and name == "vlm-judge":
            return live.VLMJudgeProvider(price=price)
        raise NotConfigured(f"no live provider configured for {kind!r} (configs/studio/providers.yaml)")

    def size_for(self, kind: str, aspect: str) -> Optional[str]:
        if self.mode != "live":
            return None
        cfg = (self.config.get("live") or {}).get(kind) or {}
        return cfg.get("size_portrait" if aspect == "9:16" else "size_landscape")
