# Moving, lip-synced shots: VieNeu voices + open-weight video models

Status date: 2026-11-05. External spend: **$0**. Everything here is **OFFLINE**-verified (ComfyUI and VieNeu
stubbed with their documented interfaces, real ffmpeg, real SQLite). **No model has run**: this container has no
GPU and cannot reach Hugging Face, where both VieNeu and the video models keep their weights.

## Why open weights fix what closed APIs could not

Every closed video API we integrated (Veo 3.1, Seedance 2.0, MiniMax H3, Wan 3.0; Sora is gone) either makes its
own audio or none: none lip-syncs to a voice track we supply. Audio-driven open models take *our* track, so the
VieNeu line, the subtitles and the mouth all come from the same file.

| Shot kind (from FilmDSL) | Route | Candidate models (license) |
|---|---|---|
| `dialogue`: a line spoken on screen | `talking` ComfyUI workflow: keyframe + dialogue track -> clip | InfiniteTalk (Apache-2.0; head, body and expression follow the audio; multi-person weights; I2V ~1 min per clip; 480p/720p), LongCat-Video-Avatar 1.5 (MIT; Whisper-large-v3 audio encoder; multi-character), Kandinsky S2V (MIT; 2B Lite / 21B Pro) |
| `action`: characters, no line | `action` ComfyUI workflow: keyframe + staging prompt -> clip | Wan 2.2 I2V A14B / TI2V-5B (Apache-2.0, silent, 5 s), LTX-2.x (community license, free under $10M revenue) |
| `establishing`: no characters | `still`: camera move over the keyframe | `motion-still` (local ffmpeg, $0) |

No source documents Vietnamese lip sync for any of them; the first GPU run must check it on our own lines.

## What the code does

- `src/studio/providers/vieneu.py` `vieneu-tts`: VieNeu-TTS v3 Turbo, in-process (`pip install vieneu`, CPU via
  ONNX) or through VieNeu's own OpenAI-shaped server (`VIENEU_BASE_URL`). `voice_map` maps planned voices to
  presets or `clone:<clip.wav>`; unmapped voices are refused; `instructions` are dropped with a note.
- `src/studio/providers/router.py` `shot-router`: picks the route from `shot_kind`, records `route` and every
  adaptation in the attempt, prefixes remote ids with the route so restarts reconcile.
- `src/studio/providers/comfyui.py` `comfyui`: uploads keyframe/audio, fills the workflow placeholders, queues
  `/prompt`, polls `/history/{id}`, downloads the video from `/view`, cancels with `/queue`.
- Engine: tags each clip with `shot_kind`; for lip-sync providers builds a **shot-length dialogue track** (lead
  silence + narration length as silence + the line + tail) so the mouth moves exactly where the export mixes the
  line, and the narrator never moves anyone's lips. The track is content-addressed: a new voice take re-renders
  the clip, nothing else does. Clips from the `still` route skip the paid judge (file check only); talking and
  action clips go to the judge.

## Your ComfyUI workflows

1. Build and test the workflow in ComfyUI (e.g. kijai's ComfyUI-WanVideoWrapper for InfiniteTalk / Wan 2.2).
2. Export with **Save (API)** to `configs/studio/comfyui/talking.json` and `configs/studio/comfyui/action.json`.
3. Replace the inputs with placeholders: the whole string value becomes the typed value.

| Placeholder | Value |
|---|---|
| `{{image}}` | uploaded keyframe file name (LoadImage `image`) |
| `{{audio}}` | uploaded dialogue track, 16 kHz mono WAV, shot length (LoadAudio `audio`); its presence marks the workflow as lip-sync |
| `{{prompt}}` `{{negative_prompt}}` | the shot's clip prompt / negative prompt |
| `{{seed}}` `{{width}}` `{{height}}` `{{fps}}` `{{duration}}` | numbers |
| `{{frames}}` | `duration x fps` rounded up to `frames_rule` (`4n+1` for Wan-family, `8n+1` for LTX) |

4. `COMFYUI_BASE_URL=http://<host>:8188`, `STUDIO_PROVIDERS=configs/studio/providers.openai_gpu.yaml`.
   `python -m src.studio providers` shows each route; a missing workflow or URL blocks that route only.

## Cost

OpenAI: keyframes and sheets as in OPENAI_ONLY.md (ceiling ~$1.3 per film minute). Voices: $0. GPU: electricity on
your own card; rented, per hour (RunPod list 2026-11-04: RTX 4090 $0.89, L40S $1.09, A100 80GB $1.79, H100 PCIe
$2.89). Speed per clip is not published for InfiniteTalk or LongCat; measure on the first pod and put the
resulting $/second in each route's `price.usd`.
