# ComfyUI workflows for the shot router

Put your API-format exports here (ComfyUI: Save (API)):

- `talking.json`: image + `{{audio}}` -> lip-synced clip (InfiniteTalk, LongCat-Video-Avatar, ...)
- `action.json`: image + `{{prompt}}` -> clip (Wan 2.2 I2V, LTX-2.x, ...)

Placeholders and the full procedure: `docs/studio/MOTION_LIPSYNC.md`. `example_talking.json` shows the
placeholder shape only (node classes are illustrative, not a runnable graph).
