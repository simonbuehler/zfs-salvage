# Demo video

A real rescue, recorded in the browser, of a pool with invented content (no real photos or people), so it can be
published. Everything is regenerated with four commands; the results land in `demo/build/` (not in git).

| Step | Command | Needs |
|---|---|---|
| 1. Content | `demo/make-content.py` | Python with Pillow and imageio-ffmpeg |
| 2. Pool | `tests/vm/run.sh demo/make-pool.sh` | the test VM (Docker, KVM); builds pool `tank`, destroys the objset block of `tank/home` |
| 3. Recording | `demo/record.py` | Playwright with Chromium, the `zfs-salvage-zdb` image (labels are read with real zdb once) |
| 4. Cut | `demo/cut.py` | imageio-ffmpeg |

Steps 1, 3 and 4 run with one Python environment, e.g.
`python3 -m venv ~/.cache/zfs-salvage-demo-venv && ~/.cache/zfs-salvage-demo-venv/bin/pip install playwright imageio-ffmpeg pillow && ~/.cache/zfs-salvage-demo-venv/bin/playwright install chromium`.

Output: `zfs-salvage-demo.mp4` (1920x1080, 30 fps, H.264, silent: README and release page), `zfs-salvage-demo-youtube.mp4`
(the same with music that `cut.py` synthesises itself, so there is no licence to clear), `zfs-salvage-demo.gif` (800x450, only
the zoomed key moments, for the README) and `thumbnail.png` (1280x720, YouTube).

The cut zooms into the moments that matter (record.py notes where the controls are), shows captions only there, plays
waiting faster and replaces the scan with an "A few hours later…" card that says how long a real scan takes. The demo pool is
built with the VM clock set to 2014, so its states carry the dates of the photos.

The recording drives the real web UI of this repository against the damaged pool: setup, scan, choosing the file system,
catalog, selection, copy and verification all happen for real. Waiting is shortened in the cut, never staged.
