# One OpenAI key: the openai-only line-up

Status date: 2026-11-05. External API spend: **$0**. Everything below is **OFFLINE**-verified (OpenAI clients
stubbed, real ffmpeg, real SQLite). **Nothing is LIVE-verified** until the canary runs with a real key.

## What changed at OpenAI (read on developers.openai.com, 2026-11-05)

| Old recipe | Now |
|---|---|
| Sora / `/v1/videos` for clips | **Shut down 2026-09-24**, no replacement model listed. |
| DALL-E 3 for images | Shut down 2026-05-12. `gpt-image-1` shut down 2026-10-23. Current: `gpt-image-2`, `gpt-image-2.5-flare/-sunburst`. |
| `tts-1` voices alloy/echo/onyx | `gpt-4o-mini-tts` with `instructions` (accent, tone); 13 voices; deprecated, shutdown 2027-01-06, no speech-endpoint successor named yet. |
| GPT-4o for the script | GPT-6 family: `gpt-6.1-terra` $1/$5 per 1M tokens, `gpt-6-luna` $0.10/$0.50. |

So with one OpenAI key there is **no OpenAI model that makes moving video**. The line-up below makes the
film from OpenAI frames + OpenAI voices, and does the camera work locally.

## The line-up (`configs/studio/providers.openai.yaml`)

| Stage | Backend | Cost |
|---|---|---|
| Script → FilmDSL (CineCrew crew) | OpenAI chat, `LLM_MODEL` (e.g. `gpt-6.1-terra`) | tokens |
| Reference sheets + 1 keyframe per shot | `gpt-image-2` (keyframes go to `/images/edits` with the asset sheets as references: same faces every shot) | ceiling $0.10/image, settled from returned token usage |
| Dialogue + narration (Vietnamese) | `gpt-4o-mini-tts`, voice + `instructions` | ceiling $0.03 / 1k chars |
| Review of sheets and keyframes | OpenAI vision chat (`judge.model`) | ceiling $0.03/call |
| Clip | **`motion-still`** (`src/studio/providers/motion.py`): the approved keyframe + a camera move from FilmDSL `camera.movement` (push-in, pull-out, pan, tilt, slow "breathing" push for static) rendered with ffmpeg at 1080x1920 | $0 |
| Edit, subtitles, mix, export | existing `src/studio/export.py` (ffmpeg) | $0 |

Clips from `motion-still` skip the paid clip review: a camera move cannot change a face, the keyframe it
moves over was already judged. They still get a file check (decodes, duration, no black segment).

## Run

```bash
export OPENAI_API_KEY=sk-...              # the only key
export LLM_MODEL=gpt-6.1-terra
export STUDIO_PROVIDERS=configs/studio/providers.openai.yaml
python -m src.studio providers            # every kind -> backend, key yes/no, price set
python -m src.studio estimate --minutes 10 --budget 10
python -m src.studio canary --budget 10   # ONE shot, real calls, hard $10 cap
```

## Cost and length

`estimate` uses the ceilings above (no planning LLM): **$1.28/min first try, $2.56/min if every job retries
once**; 10 min = $12.8 to $25.6; a $10 budget covers ≥ 3.6 min in the worst case. The real bill should be
lower: the ceilings assume ~$0.10 per image, while gpt-image-2 lists $15 per 1M output tokens (the
per-image token count for 1024x1536 is not on the pages we could read; the canary measures it).
For comparison the Seedance line-up (PR #2) is ≈ $8 to $16 per minute.

Length: no pipeline cap (the 30-min fixture episode already exported, see HUOBAO_INTEGRATION.md); one
TTS call ≤ 4096 chars; clip length is unlimited for `motion-still`.

## Known limits (honest)

- **No character motion**: people do not walk or move their lips; it is a motion comic / "Ken Burns" film.
  Mouths do not sync. Optional next step: a second keyframe per shot (end state) cross-faded, ≈ +$0.1/shot.
- Keyframes are 2:3 (1024x1536) and are cover-cropped to 9:16: a sliver of the top and bottom is cut.
- `gpt-4o-mini-tts` is deprecated (2027-01-06). When OpenAI names a successor for `/audio/speech`, change
  `tts.model`; nothing else changes.
- Prices for images and TTS are ceilings, not measured; first canary settles them.
