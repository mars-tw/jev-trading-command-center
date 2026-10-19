# Actual-footage showcase recipe

This optional tool assembles actual JPEG browser captures and the original
Neon Circuit soundtrack. It does not generate replacement application UI.
Python, FFmpeg/ffprobe and a suitable system font are external requirements.

Each segment folder contains numbered `.jpg` frames and matching `.json`
metadata with an epoch-seconds `timestamp` and numeric `sequence`. A manifest
has `segments: [{"folder": "stage", "label": "Dashboard"}]`. The optional
`skip_frames` field excludes leading capture setup frames.

The manifest must declare `privacy_confirmed: true`; use `data_mode:
"synthetic"` for the supplied offline DEMO. This is a caller declaration,
not an automatic redaction filter. Inspect and obtain permission for every
frame and audio source before sharing.

```sh
python showcase_source.py --frames ./frames --segments capture-manifest.json --music music.wav --output jev-showcase.mp4
```

The released video uses only the offline application's synthetic data,
three actual screen-recorded segments, and genuine first/last-frame title
holds. No desktop, microphone, real account or provider credentials were
recorded. See PROVENANCE.txt for the final capture/edit details.
