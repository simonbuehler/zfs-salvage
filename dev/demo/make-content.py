#!/usr/bin/env python3
"""Invented content for the demo pool: a home directory of two people ("anna", "ben") with holiday photos, documents
and a short video. Everything is generated (procedural landscapes, no real photos, no real people), so the demo video
can be published. Deterministic: the same files every time.

Usage: dev/demo/make-content.py <out dir>      (needs Pillow; the video needs imageio-ffmpeg)
Writes <out>/batch1, <out>/batch2 (copied into the pool one after the other, so the pool has older and newer states)
and <out>/files.json (path, size, sha256, mtime of every file).
"""
import hashlib
import json
import math
import os
import random
import subprocess
import sys
import time

from PIL import Image, ImageDraw, ImageFilter

W, H = 1600, 1067


def sky(d, rnd, top, bottom, horizon):
    for y in range(horizon):
        t = y / max(horizon - 1, 1)
        d.line([(0, y), (W, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))


def ridge(d, rnd, base, rough, color, step=40):
    pts, y = [(0, H)], base
    for x in range(0, W + step, step):
        y += rnd.uniform(-rough, rough)
        y = min(max(y, base - 3 * rough), base + 2 * rough)
        pts.append((x, y))
    pts.append((W, H))
    d.polygon(pts, fill=color)


def landscape(kind, seed):
    rnd = random.Random(seed)
    img = Image.new('RGB', (W, H))
    d = ImageDraw.Draw(img)
    if kind == 'iceland':
        sky(d, rnd, (96, 130, 170), (200, 214, 226), 600)
        ridge(d, rnd, 420 + rnd.randint(-40, 40), 35, (70, 78, 90))
        ridge(d, rnd, 520, 25, (52, 62, 58))
        for _ in range(3):                                        # snow
            x = rnd.randint(100, W - 300)
            d.polygon([(x, 470), (x + 120, 380 + rnd.randint(-30, 30)), (x + 240, 470)], fill=(236, 240, 244))
        ridge(d, rnd, 700, 15, (64, 96, 58))
        if rnd.random() < 0.6:                                   # waterfall
            x = rnd.randint(500, 1100)
            d.rectangle([x, 520, x + 40, 760], fill=(220, 232, 240))
        d.rectangle([0, 820, W, H], fill=(58, 86, 60))
    elif kind == 'lisbon':
        sky(d, rnd, (60, 140, 220), (170, 210, 240), 820)
        x = 0
        while x < W:                                              # houses
            w, h = rnd.randint(90, 180), rnd.randint(220, 460)
            col = rnd.choice([(232, 196, 92), (210, 110, 80), (240, 236, 224), (120, 170, 190), (226, 150, 120)])
            d.rectangle([x, 820 - h, x + w, 820], fill=col)
            d.polygon([(x - 6, 820 - h), (x + w / 2, 790 - h), (x + w + 6, 820 - h)], fill=(170, 74, 52))
            for wy in range(820 - h + 30, 800, 60):
                for wx in range(x + 15, x + w - 25, 40):
                    d.rectangle([wx, wy, wx + 18, wy + 30], fill=(60, 70, 90))
            x += w + rnd.randint(0, 8)
        d.rectangle([0, 820, W, H], fill=(160, 150, 140))
        if rnd.random() < 0.5:                                    # tram
            x = rnd.randint(100, 1200)
            d.rounded_rectangle([x, 760, x + 300, 900], 18, fill=(246, 200, 40))
            for k in range(5):
                d.rectangle([x + 20 + k * 55, 780, x + 60 + k * 55, 830], fill=(80, 100, 120))
    else:                                                        # lake
        sky(d, rnd, (250, 160, 90), (255, 220, 160), 560)
        sx = 730 + rnd.randint(-300, 300)                        # the sun
        d.ellipse([sx - 60, 380, sx + 60, 500], fill=(255, 240, 190))
        ridge(d, rnd, 470, 40, (110, 90, 120))
        ridge(d, rnd, 540, 20, (70, 70, 96))
        for y in range(560, H):                                   # water with reflection stripes
            t = (y - 560) / (H - 560)
            d.line([(0, y), (W, y)], fill=(int(220 - 120 * t), int(150 - 60 * t), int(120 + 20 * t)))
        for _ in range(60):
            y = rnd.randint(570, H - 10)
            x = rnd.randint(0, W - 200)
            d.line([(x, y), (x + rnd.randint(60, 200), y)], fill=(255, 210, 160), width=2)
    img = img.filter(ImageFilter.GaussianBlur(1.2))
    noise = Image.effect_noise((W, H), 14).convert('RGB')
    return Image.blend(img, noise, 0.06)


def video(path, seed, seconds=6):
    """A short clip of a kayak on a lake, encoded with the ffmpeg of imageio-ffmpeg."""
    import imageio_ffmpeg
    fps, w, h = 25, 960, 540
    base = landscape('lake', seed).resize((w, h))
    proc = subprocess.Popen([imageio_ffmpeg.get_ffmpeg_exe(), '-loglevel', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24',
                             '-s', f'{w}x{h}', '-r', str(fps), '-i', '-', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                             '-preset', 'slow', '-crf', '26', '-metadata', 'creation_time=2014-06-21T10:00:00Z', path],
                            stdin=subprocess.PIPE)
    for k in range(fps * seconds):
        f = base.copy()
        d = ImageDraw.Draw(f)
        x = 80 + k * 4
        y = 420 + 6 * math.sin(k / 6)
        d.polygon([(x, y), (x + 140, y - 6), (x + 160, y + 4), (x + 10, y + 10)], fill=(220, 60, 40))
        d.line([(x + 50, y - 50), (x + 110, y + 30)], fill=(40, 40, 40), width=4)
        proc.stdin.write(f.tobytes())
    proc.stdin.close()
    proc.wait()


def ts(text):
    return int(time.mktime(time.strptime(text, '%Y-%m-%d %H:%M')))


def main(out):
    os.makedirs(out, exist_ok=True)
    files = []

    def add(batch, rel, data=None, src=None, when='2014-05-01 12:00'):
        p = os.path.join(out, batch, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if data is not None:
            with open(p, 'wb') as f:
                f.write(data)
        elif src is not None:
            os.replace(src, p)
        t = ts(when)
        os.utime(p, (t, t))
        with open(p, 'rb') as f:
            body = f.read()
        files.append({'path': '/' + rel, 'size': len(body), 'sha256': hashlib.sha256(body).hexdigest(), 'mtime': t, 'batch': batch})

    def photos(batch, folder, kind, first, n, day, seed):
        for i in range(n):
            img = landscape(kind, seed * 100 + i)
            p = os.path.join(out, 'tmp.jpg')
            img.save(p, 'JPEG', quality=86)
            add(batch, f'anna/Pictures/{folder}/IMG_{first + i:04d}.JPG', src=p, when=f'{day} {9 + i % 9:02d}:{(i * 7) % 60:02d}')

    photos('batch1', '2013-08 Iceland', 'iceland', 412, 14, '2013-08-17', 1)
    photos('batch1', '2014-04 Lisbon', 'lisbon', 1101, 16, '2014-04-12', 2)
    add('batch1', 'anna/Documents/Taxes 2013.pdf', b'%PDF-1.4\n% invented demo document\n' + b'0' * 48000 + b'\n%%EOF\n',
        when='2014-03-30 20:15')
    add('batch1', "anna/Documents/Recipes/Grandma's apple cake.txt",
        b'Apple cake\n\n4 apples, 200 g flour, 150 g sugar, 3 eggs, 125 g butter.\nBake at 180 C for 45 minutes.\n',
        when='2012-11-02 18:40')
    add('batch1', 'ben/Projects/thesis/chapter1.tex',
        ''.join(f'\\section{{Part {k}}}\nInvented text for the demo, paragraph {k}.\n' for k in range(400)).encode(),
        when='2014-05-20 23:10')
    add('batch1', 'ben/Projects/thesis/notes.md', b'# Notes\n\n- read chapter 3 again\n- ask about the figures\n',
        when='2014-05-21 08:05')
    p = os.path.join(out, 'tmp.mp4')
    video(p, 7)
    add('batch2', 'ben/Videos/2014-06 Kayak trip.mp4', src=p, when='2014-06-21 10:00')
    photos('batch2', '2014-09 Lake Garda', 'lake', 2207, 12, '2014-09-06', 3)
    add('batch2', 'anna/Documents/Wedding Mia & Tom - seating plan.txt',
        ''.join(f'Table {k}: guest {2 * k}, guest {2 * k + 1}\n' for k in range(1, 13)).encode(), when='2014-09-01 21:30')
    with open(os.path.join(out, 'files.json'), 'w') as f:
        json.dump(files, f, indent=1, ensure_ascii=False)
    print(f'{len(files)} files, {sum(x["size"] for x in files) / 1e6:.1f} MB')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), 'build', 'content'))
