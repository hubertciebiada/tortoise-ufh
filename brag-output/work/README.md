# How brag.mp4 was made

Sources of the `/brag` launch video (built with `/brag-slim`, see `../brag-plan.md`).

- `video.html` + `video.mjs`: the 1920×1080 stage. Every frame is a pure function of
  time (`window.renderFrame(t)`). It imports the real logo (`scripts/brand/logo.mjs`) and
  the real sidebar panel (`custom_components/tortoise_ufh/frontend/tortoise-ufh-panel.js`,
  mounted with a mocked `hass`, like `dev/panel-preview.html`).
- `simdata.py` -> `simdata.json`: the chart data, from running the library scenario
  `steady_heating` on the built-in digital twin with the real `BuildingController`
  (peak 21.181 °C on a 21 °C setpoint, the "+0.18 K" in the video).
- `music.py`: the original soundtrack (numpy/scipy synthesis, 120 BPM, D major).
- `render.mjs`: drives headless Chromium frame by frame and pipes PNGs into ffmpeg.

Rebuild (from the repo root, with Python 3.12 + numpy/scipy/imageio-ffmpeg, and
`npm ci` in `scripts/brand` and here):

```bash
python3 -m http.server 8765 --bind 127.0.0.1 &          # serve the repo root
PYTHONPATH=. python brag-output/work/simdata.py steady_heating   # -> simdata.json
cd brag-output/work
python music.py                                         # -> soundtrack.wav
FFMPEG=$(python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())") \
  node render.mjs --out video-only.mp4                  # 630 frames -> video-only.mp4
node render.mjs --stills 6.4                            # poster frame -> stills/t06.40.png
```

Then overlay the poster on frame 0 and mux the soundtrack with ffmpeg
(`overlay=enable='eq(n,0)'`, libx264 CRF 15, AAC 192k).
