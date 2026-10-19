# Neon Circuit

Original procedural 116 BPM instrumental soundtrack used in the showcase.
The source and composition/recording use the MIT license in this folder.
No third-party song or samples are included.

Install NumPy and have FFmpeg/ffprobe on PATH, then use a new output and
a task-owned temporary directory:

```sh
python music_source.py --output music.wav --temp-dir ./music-temp
```

The completed `music.wav` is available in the v0.1.0 release assets.
Generated audio and temporary files are not part of the Git source tree.
