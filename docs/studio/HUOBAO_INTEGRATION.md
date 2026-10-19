# huobao-drama integration (provider line-up)

Status date: 2026-10-19. External API spend: **$0**. Everything below is **OFFLINE**-verified (mock HTTP
transports with the documented request shapes, real ffmpeg, real SQLite). **Nothing is LIVE-verified.**

## What was taken from huobao-drama, and how

[huobao-drama](https://github.com/chatfire-AI/huobao-drama) (commit `26de7df`, 2026-10-06) is a TypeScript
short-drama platform: novel → script → asset extraction → images → storyboard → video → ffmpeg stitch.
Its pipeline stages already exist in CineCrew (Art Department, Story Editor, Cinematographer, Studio export),
so the value for CineCrew is its **backend line-up** and its **multi-reference consistency idea**.

License: huobao-drama is **CC BY-NC-SA 4.0** (non-commercial, share-alike); CineCrew is Apache-2.0.
No code, prompt or text was copied. The adapters in `src/studio/providers/multiref.py` were written from the
providers' official API documentation (URLs in each adapter's `caps.docs`).

| From huobao-drama | In CineCrew |
|---|---|
| Seedance 2.0 video (Volcengine Ark / BytePlus) | `SeedanceVideoProvider` (`provider: seedance`) |
| MiniMax H3 video | `MiniMaxVideoProvider` (`provider: minimax-video`) |
| Alibaba Wan 3.0 video | `WanVideoProvider` (`provider: wan-video`) |
| Seedream / Gemini / OpenAI images | `SeedreamImageProvider`, `GeminiImageProvider`, existing `OpenAIImageProvider` |
| "Huobao API key" one-key gateway (api.firemux.com) | `HUOBAO_API_KEY` routing in `resolve_endpoint` |
| Asset images as labelled references for the video model | `Engine._video_references` + `[Image N] = name <asset_id>` prompt header |
| (huobao removed TTS) | `MiniMaxTTSProvider` (MiniMax T2A v2, `language_boost: Vietnamese`) |

## Using it

```bash
STUDIO_PROVIDERS=configs/studio/providers.huobao.yaml python -m src.studio providers   # keys / route / prices
STUDIO_PROVIDERS=configs/studio/providers.huobao.yaml python -m src.studio estimate --minutes 10 --budget 100
STUDIO_PROVIDERS=configs/studio/providers.huobao.yaml python -m src.studio canary --budget 10
```

Keys (env / `.env`): `ARK_API_KEY` (Seedance + Seedream), `MINIMAX_API_KEY` (H3 video + speech),
`DASHSCOPE_API_KEY` + `DASHSCOPE_BASE_URL` (Wan 3.0), `GEMINI_API_KEY` (Gemini images), `LLM_API_KEY`
(+ `LLM_BASE_URL`, `LLM_MODEL`) for planning and the visual judge. A direct key wins; without it,
`HUOBAO_API_KEY` routes through the gateway at `HUOBAO_BASE_URL` (default `https://api.firemux.com`) under
`/volcengine`, `/minimax`, `/qwen`, `/v1`. **The gateway paths come from huobao-drama's settings page; the
gateway publishes no readable API docs or prices, so this route is UNCONFIRMED** until a canary passes on it.

## Behaviour decisions (each one recorded as an adaptation on the attempt)

- **First frame vs references.** All three video APIs document first-frame mode and reference mode as
  mutually exclusive. Default `video_mode: first_frame`: the approved keyframe (itself rendered from the
  asset sheets) drives the clip; no sheets are sent. `video_mode: references`: the asset sheets plus the
  approved keyframe go as reference images and the prompt starts with
  `Reference images: [Image 1] Minh <char_minh>; ...`. Which gives better identity is a LIVE question.
- **Durations** are whole seconds: Seedance 4–15, MiniMax H3 4–15, Wan 3.0 2–30. Planned slots are rounded up;
  assembly trims to the slot. Longer shots are clamped to the maximum (adaptation note).
- **Audio**: `generate_audio: false` (Seedance) / `audio: false` (Wan); the studio mixes its own TTS. MiniMax H3
  has no audio switch. No backend here lip-syncs to our voice track.
- **Images go inline** as base64 data URIs (documented as accepted by all three), so no public file server.
- **Cancel**: Seedance and MiniMax cancel queued tasks (`DELETE`); a running task finishes and bills.
- **Cost**: reservation per second before submit; Seedance settles from `usage.total_tokens` × `usd_per_1m_tokens`.
- **TTS voices**: planned voice names map to MiniMax `voice_id`s via `voice_map`; an unmapped voice is refused
  before any call.

## Length limits (measured vs inferred)

| Limit | Value | Source |
|---|---|---|
| One clip | Seedance/MiniMax 15 s, Wan 30 s | provider docs |
| One episode, export | 10 min (60 shots): render 88 s + export 226 s, 405 MB RAM, 4 CPU, all checks pass | **measured OFFLINE** (fixture media) |
| One episode, planning | ≤ 10 000 script characters per DSL segment (`limits.script_segment_dsl`) | config |
| Whole film | N episodes, no pipeline cap | design |
| Money | Seedance 2.0 fast 720p ≈ $0.121/s of clip (21 600 tok/s × $5.6/1M) | price page + arithmetic |

## Offline QC

`tests/test_multiref_providers.py` (contract tests per provider: request shape, auth, gateway URL, errors 429/5xx/4xx,
HTTP-200 refusals, network drop → ambiguous, poll states, URL expiry, cancel, cost settlement, voice map) and
`tests/test_multiref_engine.py` (engine end to end over mock HTTP: sheets reach the request in reference mode,
one submission per clip, restart mid-render reconciles without resubmitting, ledger settles, export final).
