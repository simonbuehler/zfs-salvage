#!/usr/bin/env python3
"""Record the demo: a real rescue of the demo pool (dev/demo/build/tank.img.xz) through the web UI, clicked by a browser.

The browser is Chromium driven by Playwright; frames come from the DevTools screencast (sharp, at 1920x1080), so nothing
is re-rendered. Headless Chromium has no mouse pointer: a drawn pointer moves to every control before it is clicked.
Output in dev/demo/build/rec/: frames/*.jpg, frames.json (file, time) and events.json (scene marks, fast-forward ranges),
which dev/demo/cut.py turns into the video.

Usage: dev/demo/record.py          (with the Python of the demo environment: Playwright, Pillow)
"""
import json
import lzma
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
BUILD = os.path.join(HERE, 'build')
REC = os.path.join(BUILD, 'rec')
WORK = os.path.expanduser('~/zfs-demo')               # the working folder on this machine ...
SHOWN = '/mnt/target'                                 # ... and where the server (in a container) sees it: in the video
IMAGE = 'zfs-salvage-dev'                             # the dev container image (.devcontainer/Dockerfile)
TOKEN = 'demo-session'
PORT = 8799

CURSOR = r'''
(() => {
  const add = () => {
    if (document.getElementById('demo-cursor')) return;
    const c = document.createElement('div');
    c.id = 'demo-cursor';
    c.innerHTML = '<svg width="26" height="26" viewBox="0 0 26 26"><path d="M3 2 L3 21 L8 16 L12 24 L15 23 L11 15 L18 15 Z" fill="#111" stroke="#fff" stroke-width="1.6"/></svg>';
    Object.assign(c.style, {position: 'fixed', zIndex: 2147483647, pointerEvents: 'none', left: '0', top: '0', scale: '1.4',
                            transform: 'translate(' + (sessionStorage.cx || 960) + 'px,' + (sessionStorage.cy || 600) + 'px)'});
    document.documentElement.appendChild(c);
  };
  window.__demoMove = (x, y, ms) => new Promise(done => {
    add();
    const c = document.getElementById('demo-cursor');
    const x0 = +(sessionStorage.cx || 960), y0 = +(sessionStorage.cy || 600), t0 = performance.now();
    const step = t => {
      const k = Math.min(1, (t - t0) / ms), e = k < .5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
      c.style.transform = `translate(${x0 + (x - x0) * e}px,${y0 + (y - y0) * e}px)`;
      if (k < 1) requestAnimationFrame(step); else { sessionStorage.cx = x; sessionStorage.cy = y; done(); }
    };
    requestAnimationFrame(step);
  });
  window.__demoClick = (x, y) => {
    const r = document.createElement('div');
    Object.assign(r.style, {position: 'fixed', left: (x - 18) + 'px', top: (y - 18) + 'px', width: '36px', height: '36px',
      borderRadius: '50%', border: '3px solid #2f6f4f', zIndex: 2147483646, pointerEvents: 'none', transition: 'all .45s ease-out'});
    document.documentElement.appendChild(r);
    requestAnimationFrame(() => { r.style.transform = 'scale(1.8)'; r.style.opacity = '0'; });
    setTimeout(() => r.remove(), 500);
  };
  const zoom = () => { if (document.body) document.body.style.zoom = '1.5'; };   // 1280x720 layout, rendered at 1920x1080
  const init = () => { zoom(); add(); };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
'''


def prepare():
    """A fresh working folder: the damaged disk image, an empty data folder, and zdb answers recorded from real zdb."""
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(f'{WORK}/disks')
    os.makedirs(f'{WORK}/bin')
    img = f'{WORK}/disks/old-nas-disk.img'
    with lzma.open(os.path.join(BUILD, 'tank.img.xz')) as src, open(img, 'wb') as dst:
        shutil.copyfileobj(src, dst)
    rec = {}
    for flag in ('-l', '-luuuu'):                         # real zdb 2.2.2 (container), recorded once
        rec[flag] = subprocess.run(['docker', 'run', '--rm', '-v', f'{WORK}/disks:/d:ro', 'zfs-salvage-zdb', 'zdb', flag,
                                    '/d/old-nas-disk.img'], capture_output=True, text=True, check=True).stdout
        with open(f'{WORK}/bin/zdb{flag}.txt', 'w') as f:
            f.write(rec[flag])
    with open(f'{WORK}/bin/zdb', 'w') as f:
        f.write(f'#!/bin/bash\ncase "$1" in -l|-luuuu) exec cat "{SHOWN}/bin/zdb$1.txt";; *) exit 1;; esac\n')
    os.chmod(f'{WORK}/bin/zdb', 0o755)
    return f'{SHOWN}/disks/old-nas-disk.img'


def start_server():
    """The web UI in a container, like on a live system: the target disk at /mnt/target, no drives of this machine."""
    env = dict(HOME=SHOWN, ZFS_SALVAGE_STATE_DIR=f'{SHOWN}/data', ZFS_SALVAGE_TOKEN=TOKEN, SALVAGE_UI_PORT=str(PORT),
               ZFS_SALVAGE_ZDB=f'{SHOWN}/bin/zdb', ZFS_SALVAGE_SMARTCTL='/nonexistent', ZFS_SALVAGE_DOCTOR_FAKE='{"lsblk": ""}')
    subprocess.run(['docker', 'rm', '-f', 'zfs-salvage-demo'], capture_output=True)
    proc = subprocess.Popen(['docker', 'run', '--rm', '--name', 'zfs-salvage-demo', '--network', 'host',
                             '--user', f'{os.getuid()}:{os.getgid()}', '-v', f'{WORK}:{SHOWN}', '-v', f'{ROOT}:/opt/zfs-salvage:ro',
                             *[a for k, v in env.items() for a in ('-e', f'{k}={v}')], IMAGE, '/opt/zfs-salvage/zfs-salvage', 'web'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            socket.create_connection(('127.0.0.1', PORT), timeout=1).close()
            return proc
        except OSError:
            time.sleep(0.2)
    raise SystemExit('the web server did not start')


def api(path):
    req = urllib.request.Request(f'http://127.0.0.1:{PORT}{path}', headers={'X-ZS-Token': TOKEN})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def until(fn, seconds=240):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if fn():
                return
        except Exception:  # noqa: BLE001 - the server restarts once during setup
            pass
        time.sleep(0.4)
    raise SystemExit('timeout')


class Rec:
    def __init__(self, page):
        self.page, self.events, self.frames = page, [], []
        os.makedirs(f'{REC}/frames')
        self.cdp = page.context.new_cdp_session(page)
        self.cdp.on('Page.screencastFrame', self.frame)
        self.cdp.send('Page.startScreencast', {'format': 'jpeg', 'quality': 92, 'maxWidth': 1920, 'maxHeight': 1080})

    def frame(self, f):
        name = f'{len(self.frames):06d}.jpg'
        import base64
        with open(f'{REC}/frames/{name}', 'wb') as out:
            out.write(base64.b64decode(f['data']))
        self.frames.append((name, f['metadata']['timestamp']))
        self.cdp.send('Page.screencastFrameAck', {'sessionId': f['sessionId']})

    def mark(self, name):
        self.events.append((name, time.time()))

    def rect(self, name, loc, pad=24):
        """Where a control is on the screen now (frame pixels): the cut zooms there."""
        b = loc.bounding_box()
        self.events.append((f'rect:{name}', time.time(), [b['x'] - pad, b['y'] - pad, b['width'] + 2 * pad, b['height'] + 2 * pad]))

    def hold(self, s):
        end = time.time() + s
        while time.time() < end:
            self.page.wait_for_timeout(100)

    def point(self, loc, ms=700):
        loc.scroll_into_view_if_needed()
        b = loc.bounding_box()
        x, y = b['x'] + min(b['width'] / 2, 60), b['y'] + b['height'] / 2
        self.page.evaluate('([x, y, ms]) => window.__demoMove(x, y, ms)', [x, y, ms])
        return x, y

    def click(self, loc, pause=0.25):
        x, y = self.point(loc)
        self.hold(pause)
        self.page.evaluate('([x, y]) => window.__demoClick(x, y)', [x, y])
        loc.click()

    def type(self, loc, text):
        self.click(loc)
        loc.type(text, delay=55)

    def scroll_to(self, y, ms=900):
        self.page.evaluate('([y]) => window.scrollTo({top: y, behavior: "smooth"})', [y])
        self.hold(ms / 1000)

    def save(self):
        self.cdp.send('Page.stopScreencast')
        with open(f'{REC}/frames.json', 'w') as f:
            json.dump(self.frames, f)
        with open(f'{REC}/events.json', 'w') as f:
            json.dump(self.events, f)


def gallery(dest, shown):
    """A last page: the rescued photos, as files on the target disk."""
    pics = []
    for dirpath, _, names in os.walk(dest):
        pics += [os.path.join(dirpath, n) for n in sorted(names) if n.endswith('.JPG')]
    videos = sum(len([n for n in names if n.endswith('.mp4')]) for _, _, names in os.walk(dest))
    cells = ''.join(f'<img src="file://{p}">' for p in sorted(pics)[:40])
    html = f'''<!doctype html><meta charset="utf-8"><style>
body{{margin:0;background:#151614;color:#e8e8e3;font:20px system-ui,sans-serif}} h1{{font-size:30px;margin:28px 34px 6px}}
p{{margin:0 34px 18px;color:#9a9a93}} .g{{display:grid;grid-template-columns:repeat(8,1fr);gap:8px;padding:0 34px}}
img{{width:100%;aspect-ratio:3/2;object-fit:cover;border-radius:6px}}</style>
<h1>All {len(pics) + videos} photos and videos are back on a safe disk</h1>
<p>{shown} · original names and dates · every block checked against its ZFS checksum, every file hashed again on the target</p>
<div class="g">{cells}</div>'''
    path = f'{WORK}/gallery.html'
    with open(path, 'w') as f:
        f.write(html)
    return path


def main():
    shutil.rmtree(REC, ignore_errors=True)
    img = prepare()
    server = start_server()
    dest = f'{SHOWN}/Rescued'
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={'width': 1920, 'height': 1080}, device_scale_factor=1,
                                      color_scheme='light', locale='en-US')
            ctx.add_init_script(CURSOR)
            page = ctx.new_page()
            r = Rec(page)

            # ---- 0 set up
            r.mark('setup')
            page.goto(f'http://localhost:{PORT}/setup?t={TOKEN}')
            r.hold(1.5)
            r.type(page.locator('#extra'), img)
            r.click(page.get_by_role('button', name='Add'))
            r.hold(0.6)
            r.click(page.get_by_role('button', name='make read-only'))
            r.hold(0.6)
            r.click(page.locator('#probe'))
            until(lambda: page.locator('#poolcard').is_visible() and page.locator('input[name=ds]').count() > 1)
            r.scroll_to(page.locator('#poolcard').bounding_box()['y'] + page.evaluate('scrollY') - 20, 700)
            r.rect('datasets', page.locator('#poolcard'))
            r.hold(3.5)
            r.click(page.locator('#mkcase'))
            r.mark('fast')
            until(lambda: page.locator('#casecard').is_visible())
            r.mark('normal')
            r.scroll_to(page.locator('#casecard').bounding_box()['y'] + page.evaluate('scrollY') - 20, 700)
            r.rect('diagnosis', page.locator('#case'))
            r.hold(4.5)
            r.click(page.locator('#scan'))
            r.mark('hours')                                  # the cut puts its "a few hours later" card here
            until(lambda: api('/api/setup/state')['jobs']['rank']['state'] == 'done' and api('/api/target/groups')['groups'])
            r.hold(2)
            r.mark('normal')
            r.hold(1)

            # ---- 1 find a state
            r.mark('status')
            page.goto(f'http://localhost:{PORT}/')
            r.hold(1.5)
            r.rect('groups', page.locator('#hero'))
            r.hold(2.5)
            r.click(page.locator('button', has_text='This one').first)
            page.wait_for_load_state('load')
            until(lambda: page.locator('#hero button[onclick="rebuild()"]').count() > 0, 60)
            r.hold(1.5)
            chain = page.locator('#chain')
            r.scroll_to(chain.bounding_box()['y'] + page.evaluate('scrollY') - 10, 1200)
            r.rect('chain', page.locator('#chain svg'), pad=12)
            r.hold(7)
            r.scroll_to(0, 1000)
            r.hold(0.8)
            r.click(page.locator('#hero button[onclick="rebuild()"]'))
            r.mark('fast')
            until(lambda: api('/api/catalog/status')['state'] == 'done')
            page.wait_for_timeout(3500)                     # the page reloads itself when the catalog is ready
            r.mark('normal')
            r.rect('best', page.locator('#hero'))
            r.hold(3)

            # ---- 2 select files
            r.mark('select')
            page.goto(f'http://localhost:{PORT}/select')
            r.hold(1.5)
            for folder in ('/anna', '/anna/Pictures'):
                tw = page.locator(f'.row[data-p="{folder}"] .tw')
                if tw.count():
                    r.click(tw.first)
                    r.hold(0.8)
            r.click(page.get_by_role('button', name='📷 Photos & videos'))
            r.hold(1.5)
            r.click(page.get_by_role('button', name='select all matches'))
            r.hold(3)

            # ---- 3 transfer and verify
            r.mark('transfer')
            page.goto(f'http://localhost:{PORT}/transfer')
            r.hold(1.5)
            r.click(page.locator('.tile[data-t=local]'))
            r.type(page.locator('#path'), dest)
            r.hold(1.5)
            r.click(page.locator('#go'))
            r.mark('fast')
            until(lambda: any(t['state'] == 'done' and (t.get('verify') or {}).get('state') == 'done' for t in api('/api/transfer/status')))
            page.wait_for_timeout(3000)
            r.mark('normal')
            r.point(page.locator('#jobs'))
            r.rect('verify', page.locator('#jobs .job').first)
            r.hold(5)

            # ---- the rescued photos
            r.mark('gallery')
            page.goto('file://' + gallery(f'{WORK}/Rescued', dest))
            r.hold(6)
            r.mark('end')
            r.save()
            browser.close()
    finally:
        subprocess.run(['docker', 'stop', 'zfs-salvage-demo'], capture_output=True)
        server.wait(timeout=30)
    print(f'{len(r.frames)} frames, {r.events[-1][1] - r.events[0][1]:.0f} s, scenes {[e[0] for e in r.events]}')


if __name__ == '__main__':
    main()
