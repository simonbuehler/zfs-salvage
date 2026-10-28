#!/usr/bin/env python3
"""Cut the demo video from the recording of dev/demo/record.py: a frame-by-frame compositor (Pillow) that feeds ffmpeg.

Output in dev/demo/build/:
  zfs-salvage-demo.mp4           1920x1080, 30 fps, H.264, silent (README, release page)
  zfs-salvage-demo-youtube.mp4   the same with music (generated here, no licence questions)
  zfs-salvage-demo.gif           800x450: only the zoomed key moments (README)
  thumbnail.png                  1280x720 (YouTube)

The cut zooms into the moments that matter (where record.py noted the position of a control), shows captions only
there, plays waiting faster and replaces the scan itself with an "A few hours later…" card that says how long a real
scan takes. Everything shown happened; only time is shortened.
"""
import bisect
import json
import math
import os
import subprocess
import wave

import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, 'build')
REC = os.path.join(BUILD, 'rec')
FF = imageio_ffmpeg.get_ffmpeg_exe()
W, H, FPS = 1920, 1080, 30
FONT = '/usr/share/fonts/truetype/ubuntu/Ubuntu[wdth,wght].ttf'
MONO = '/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf'
GREEN, DARK, LIGHT, MUTED, RED = (127, 194, 155), (21, 22, 20), (236, 236, 230), (154, 154, 147), (240, 110, 110)
XFADE = 0.35
EASE = 0.8                      # seconds a zoom takes to go in or out


def font(size, weight='M'):
    """Ubuntu in the given weight (the variable font: the Ubuntu-B/-M files of the system are all Regular)."""
    f = ImageFont.truetype(FONT, size)
    f.set_variation_by_name({'B': 'Bold', 'M': 'Medium', 'R': 'Regular'}[weight])
    return f


def ease(k):
    k = min(max(k, 0.0), 1.0)
    return k * k * (3 - 2 * k)


# ---------------------------------------------------------------- the recording
class Recording:
    def __init__(self):
        with open(os.path.join(REC, 'frames.json')) as f:
            self.frames = json.load(f)
        with open(os.path.join(REC, 'events.json')) as f:
            self.events = json.load(f)
        self.times = [t for _, t in self.frames]
        self.cache = {}

    def at(self, name, nth=0):
        return [e for e in self.events if e[0] == name][nth]

    def t(self, name):
        return self.at(name)[1]

    def next_after(self, t, name):
        return min(e[1] for e in self.events if e[0] == name and e[1] > t)

    def frame(self, t):
        i = max(bisect.bisect_right(self.times, t) - 1, 0)
        name = self.frames[i][0]
        if name not in self.cache:
            if len(self.cache) > 40:
                self.cache.clear()
            self.cache[name] = Image.open(os.path.join(REC, 'frames', name)).convert('RGB')
        return self.cache[name]


def fit(rect, bottom_room=0.25, min_w=900):
    """The crop for a zoom: the control plus room below it for the caption, widened to 16:9, inside the frame."""
    x, y, w, h = rect
    h = h * (1 + bottom_room)
    cx, cy = x + w / 2, y + h / 2
    if w / h < W / H:
        w = h * W / H
    else:
        h = w * H / W
    if w < min_w:
        w, h = min_w, min_w * H / W
    w, h = min(w, W), min(h, H)
    return (min(max(cx - w / 2, 0), W - w), min(max(cy - h / 2, 0), H - h), w, h)


# ---------------------------------------------------------------- shots
class Card:
    """A still image with a slow push-in, so it does not stand still."""

    def __init__(self, img, dur, push=0.035):
        self.img, self.dur, self.push = img, dur, push
        self.crop = None

    def render(self, t):
        s = 1 + self.push * t / self.dur
        w, h = W / s, H / s
        return self.img.resize((W, H), Image.BICUBIC, box=((W - w) / 2, (H - h) / 2, (W + w) / 2, (H + h) / 2))


class Source:
    """A stretch of the recording [t0, t1), played `speed` times faster, optionally zoomed into `rect`."""

    def __init__(self, rec, t0, t1, speed=1.0, rect=None, caption=None, label=None):
        self.rec, self.t0, self.t1, self.speed = rec, t0, t1, speed
        self.crop = fit(rect) if rect else None
        top = False
        if rect and self.crop:                                   # the control sits low in the zoom: caption above it
            top = (rect[1] + rect[3] - self.crop[1]) / self.crop[3] * H > H - 300
        self.caption = caption_layer(label, caption, top) if caption else None
        self.dur = (t1 - t0) / speed

    def render(self, t):
        img = self.rec.frame(self.t0 + t * self.speed)
        if self.crop:
            k = ease(t / EASE) * ease((self.dur - t) / EASE) if self.dur > 2 * EASE else 1
            x, y, w, h = self.crop
            box = (x * k, y * k, (x + w) * k + W * (1 - k), (y + h) * k + H * (1 - k))
            img = img.resize((W, H), Image.LANCZOS, box=box)
        if self.caption:
            a = ease((t - 0.5) / 0.5) * ease((self.dur - t) / 0.4)
            if a > 0:
                layer = self.caption
                if a < 1:
                    layer = layer.copy()
                    layer.putalpha(layer.getchannel('A').point(lambda v: int(v * a)))
                img = img.copy()
                img.paste(layer, (0, 0), layer)
        return img


def wrap(text, f, width, d):
    lines, cur = [], ''
    for w in text.split():
        test = (cur + ' ' + w).strip()
        if d.textlength(test, font=f) > width and cur:
            lines.append(cur)
            cur = w
        else:
            cur = test
    return lines + [cur]


def caption_layer(label, text, top=False):
    img = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fl, ft = font(32, 'B'), font(42, 'M')
    lines = wrap(text, ft, 1500, d)
    width = max([d.textlength(label or '', font=fl)] + [d.textlength(l, font=ft) for l in lines]) + 90
    height = 44 + (46 if label else 0) + 56 * len(lines) + 20
    x0, y0 = (W - width) / 2, (50 if top else H - height - 50)
    d.rounded_rectangle([x0, y0, x0 + width, y0 + height], 24, fill=(21, 22, 20, 232))
    y = y0 + 26
    if label:
        d.text((x0 + 45, y), label, font=fl, fill=GREEN + (255,))
        y += 46
    for line in lines:
        d.text((x0 + 45, y), line, font=ft, fill=LIGHT + (255,))
        y += 56
    return img


# ---------------------------------------------------------------- cards
def centered(d, y, text, f, fill):
    d.text(((W - d.textlength(text, font=f)) / 2, y), text, font=f, fill=fill)


def hook_card(zdb_line):
    img = Image.new('RGB', (W, H), DARK)
    d = ImageDraw.Draw(img)
    centered(d, 330, 'Ten years of family photos.', font(84, 'B'), LIGHT)
    centered(d, 450, 'And then ZFS says:', font(48, 'M'), MUTED)
    mono = ImageFont.truetype(MONO, 40)
    tw = d.textlength(zdb_line, font=mono)
    box = [(W - tw) / 2 - 50, 570, (W + tw) / 2 + 50, 680]
    d.rounded_rectangle(box, 18, fill=(8, 8, 8), outline=(208, 59, 59), width=4)
    d.text((box[0] + 50, 600), zdb_line, font=mono, fill=RED)
    return img


def title_card():
    img = Image.new('RGB', (W, H), DARK)
    d = ImageDraw.Draw(img)
    centered(d, 380, 'zfs-salvage', font(140, 'B'), LIGHT)
    centered(d, 570, 'Get your files back from a ZFS dataset that no longer opens.', font(46, 'M'), GREEN)
    centered(d, 650, 'Read-only. Every block proven by its checksum.', font(38, 'R'), MUTED)
    return img


def hours_card():
    """In the style of the "A few hours later…" time cards: a sun burst and big outlined letters."""
    img = Image.new('RGB', (W, H), (255, 196, 40))
    d = ImageDraw.Draw(img)
    cx, cy = W / 2, H / 2 - 40
    for k in range(24):                                          # rays
        a0, a1 = 2 * math.pi * k / 24, 2 * math.pi * (k + 0.5) / 24
        d.polygon([(cx, cy), (cx + 2000 * math.cos(a0), cy + 2000 * math.sin(a0)),
                   (cx + 2000 * math.cos(a1), cy + 2000 * math.sin(a1))], fill=(255, 150, 30))
    glow = Image.new('L', (W, H), 0)
    ImageDraw.Draw(glow).ellipse([cx - 520, cy - 300, cx + 520, cy + 300], fill=255)
    img.paste((255, 226, 90), (0, 0), glow.filter(ImageFilter.GaussianBlur(80)))
    text = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    td = ImageDraw.Draw(text)
    f = font(150, 'B')
    msg = 'A few hours later…'
    td.text(((W - td.textlength(msg, font=f)) / 2, cy - 100), msg, font=f, fill=(255, 255, 255),
            stroke_width=14, stroke_fill=(70, 40, 120))
    text = text.rotate(-4, resample=Image.BICUBIC, center=(cx, cy))
    img.paste(text, (0, 0), text)
    note = 'A real scan reads the whole disk: about 5 hours for 3 TB. This demo pool is tiny.'
    fn = font(38, 'M')
    nw = d.textlength(note, font=fn)
    d.rounded_rectangle([(W - nw) / 2 - 30, H - 170, (W + nw) / 2 + 30, H - 100], 16, fill=(70, 40, 120))
    d.text(((W - nw) / 2, H - 158), note, font=fn, fill=(255, 255, 255))
    return img


def end_card():
    img = Image.new('RGB', (W, H), DARK)
    d = ImageDraw.Draw(img)
    centered(d, 360, 'zfs-salvage', font(120, 'B'), LIGHT)
    centered(d, 530, 'read-only · runs from an Ubuntu live USB · open source (MIT)', font(44, 'M'), GREEN)
    centered(d, 640, 'This demo: an invented pool with an invented home folder. The rescue in it is real.', font(34, 'R'), MUTED)
    return img


def thumbnail(path):
    """YouTube preview, 1280x720: the promise in big letters on the left, the story (error -> photos back) on the right."""
    tw, th = 1280, 720
    img = Image.new('RGB', (tw, th), DARK)
    d = ImageDraw.Draw(img)
    y = 150
    for text, col in (("Can't open your", LIGHT), ('ZFS dataset?', LIGHT), ('Get the files', GREEN), ('back.', GREEN)):
        d.text((60, y), text, font=font(76, 'B'), fill=col)
        y += 92
    d.text((62, y + 26), 'zfs-salvage · read-only · open source', font=font(30, 'M'), fill=MUTED)
    x0 = 700
    d.rounded_rectangle([x0, 70, tw - 50, 150], 14, fill=(10, 10, 10), outline=(208, 59, 59), width=3)
    d.text((x0 + 26, 96), 'tank/home: I/O error', font=ImageFont.truetype(MONO, 26), fill=RED)
    d.polygon([(x0 + 255, 172), (x0 + 275, 172), (x0 + 275, 196), (x0 + 292, 196), (x0 + 265, 224), (x0 + 238, 196),
               (x0 + 255, 196)], fill=MUTED)
    pics = []
    for dirpath, _, names in os.walk(os.path.join(BUILD, 'content')):
        pics += [os.path.join(dirpath, n) for n in sorted(names) if n.endswith('.JPG')]
    pics = sorted(pics)
    cw, ch, gap = 168, 112, 12
    for k, p in enumerate([pics[i] for i in (1, 16, 33, 5, 20, 40) if i < len(pics)]):
        x, yy = x0 + (k % 3) * (cw + gap), 250 + (k // 3) * (ch + gap)
        mask = Image.new('L', (cw, ch), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, cw - 1, ch - 1], 10, fill=255)
        img.paste(Image.open(p).convert('RGB').resize((cw, ch)), (x, yy), mask)
    bx, by = x0 + 3 * (cw + gap) - 52, 250 + 2 * (ch + gap) - 46
    d.ellipse([bx, by, bx + 64, by + 64], fill=(12, 163, 12), outline=DARK, width=4)
    d.line([(bx + 16, by + 33), (bx + 28, by + 45), (bx + 49, by + 21)], fill=(255, 255, 255), width=7)
    d.text((x0, 520), 'All photos and videos back,', font=font(30, 'M'), fill=LIGHT)
    d.text((x0, 560), 'every block verified', font=font(30, 'M'), fill=LIGHT)
    img.save(path)


# ---------------------------------------------------------------- the edit
def edit(rec, zdb_line):
    """The shots of the full video. Captions only where the picture is zoomed, so they never cover what matters."""
    r = rec.t

    def fast_end(t):
        return rec.next_after(t, 'normal')
    datasets, diagnosis = rec.at('rect:datasets'), rec.at('rect:diagnosis')
    groups, chain, best, verify = rec.at('rect:groups'), rec.at('rect:chain'), rec.at('rect:best'), rec.at('rect:verify')
    t_case, t_catalog, t_copy = rec.at('fast', 0)[1], rec.at('fast', 1)[1], rec.at('fast', 2)[1]
    return [
        Card(hook_card(zdb_line), 4.5),
        Card(title_card(), 3.5),
        Source(rec, r('setup'), datasets[1], speed=1.8),
        Source(rec, datasets[1], t_case, rect=datasets[2], label='Step 0 · Set up',
               caption='The pool still opens – but tank/home cannot be read.'),
        Source(rec, t_case, fast_end(t_case), speed=6),
        Source(rec, fast_end(t_case), diagnosis[1], speed=1.5),
        Source(rec, diagnosis[1], r('hours'), rect=diagnosis[2], label='Diagnosis',
               caption='It says where the pool is broken – and that it can work around exactly that.'),
        Card(hours_card(), 3.2, push=0.06),
        Source(rec, fast_end(r('hours')) - 1.0, r('status')),
        Source(rec, r('status'), groups[1]),
        Source(rec, groups[1], groups[1] + 3.0, rect=groups[2], label='Step 1 · Find a state',
               caption='The scan found the file systems on the disk. Pick yours.'),
        Source(rec, groups[1] + 3.0, chain[1], speed=1.6),
        Source(rec, chain[1], chain[1] + 7.0, rect=chain[2], label='How it works',
               caption='The chain breaks at one block. The scan goes around it and finds the file tree directly – '
                       'every block proven by its checksum.'),
        Source(rec, chain[1] + 7.0, t_catalog, speed=1.5),
        Source(rec, t_catalog, fast_end(t_catalog), speed=6),
        Source(rec, best[1], best[1] + 3.0, rect=best[2], label='Step 1 · Find a state',
               caption='The newest complete state of the folders – October 2014.'),
        Source(rec, r('select'), r('transfer') - 2.5, speed=1.3),
        Source(rec, r('transfer') - 2.5, r('transfer'), rect=[210, 150, 1500, 760], label='Step 2 · Choose files',
               caption='Pick what you want back: by folder, type, name or date.'),
        Source(rec, r('transfer'), t_copy, speed=1.3),
        Source(rec, t_copy, fast_end(t_copy), speed=6),
        Source(rec, verify[1], verify[1] + 4.5, rect=verify[2], label='Step 3 · Copy and verify',
               caption='Copied to a safe disk – then every file is hashed again on the target and compared.'),
        Source(rec, r('gallery'), r('end'), label='Done', caption='Every file back, byte for byte, with its original name and date.'),
        Card(end_card(), 5),
    ]


def gif_edit(rec):
    """The README GIF: the zoomed moments, the card and the result – no full pages, they would not be readable."""
    return [s for s in edit(rec, '') if s.crop or (isinstance(s, Card) and s.push > 0.05) or
            (isinstance(s, Source) and s.caption is not None)]


# ---------------------------------------------------------------- rendering
def render(shots, out_args, scale=None, fps=FPS):
    starts, t = [], 0.0
    for s in shots:
        starts.append(t)
        t += s.dur - XFADE
    total = t + XFADE
    size = scale or (W, H)
    proc = subprocess.Popen([FF, '-loglevel', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{size[0]}x{size[1]}',
                             '-r', str(fps), '-i', '-', *out_args], stdin=subprocess.PIPE)
    for n in range(int(total * fps)):
        t = n / fps
        i = max(k for k, st in enumerate(starts) if st <= t)
        img = shots[i].render(min(t - starts[i], shots[i].dur - 1e-3))
        if i > 0 and t - starts[i] < XFADE:                      # cross-fade from the previous shot
            prev = shots[i - 1].render(min(t - starts[i - 1], shots[i - 1].dur - 1e-3))
            img = Image.blend(prev, img, (t - starts[i]) / XFADE)
        if scale:
            img = img.resize(scale, Image.LANCZOS)
        proc.stdin.write(img.tobytes())
    proc.stdin.close()
    proc.wait()
    return total


def music(path, seconds):
    """A calm pad: A minor – F – C – G, four seconds each, with a soft arpeggio; fades in and out."""
    sr = 48000
    t = np.arange(int(sr * seconds)) / sr

    def note(m):
        return 440 * 2 ** ((m - 69) / 12)
    chords = [(57, 60, 64), (53, 57, 60), (48, 52, 55), (55, 59, 62)]
    left, right = np.zeros_like(t), np.zeros_like(t)
    for k in range(int(seconds // 4) + 1):
        ch = chords[k % 4]
        a, b = k * 4.0, k * 4.0 + 5.5
        env = np.clip((t - a) / 1.2, 0, 1) * np.clip((b - t) / 1.5, 0, 1)
        for m in ch:
            f = note(m)
            left += env * (np.sin(2 * np.pi * f * t) + 0.5 * np.sin(2 * np.pi * f * 1.003 * t)) / 6
            right += env * (np.sin(2 * np.pi * f * t) + 0.5 * np.sin(2 * np.pi * f * 0.997 * t)) / 6
        for j, m in enumerate((ch[0] + 12, ch[1] + 12, ch[2] + 12, ch[1] + 12) * 2):      # arpeggio
            on = a + j * 0.5
            pl = np.where(t >= on, np.exp(-(t - on) * 4.0), 0) * np.sin(2 * np.pi * note(m) * t) * 0.12
            left += pl * (0.8 if j % 2 else 1.0)
            right += pl * (1.0 if j % 2 else 0.8)
    fade = np.clip(t / 2.0, 0, 1) * np.clip((seconds - t) / 3.0, 0, 1)
    st = np.stack([left, right], axis=1) * fade[:, None]
    st = st / np.abs(st).max() * 10 ** (-16 / 20)
    with wave.open(path, 'wb') as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((st * 32767).astype('<i2').tobytes())


def main():
    rec = Recording()
    with open(os.path.join(BUILD, 'tank.json')) as f:
        zdb_line = [l for l in json.load(f)['zdb_after_damage'].splitlines() if l.strip()][-1]
    x264 = ['-c:v', 'libx264', '-preset', 'slow', '-crf', '18', '-pix_fmt', 'yuv420p', '-movflags', '+faststart']
    silent = os.path.join(BUILD, 'zfs-salvage-demo.mp4')
    total = render(edit(rec, zdb_line), [*x264, silent])
    wav = os.path.join(BUILD, 'music.wav')
    music(wav, total)
    youtube = os.path.join(BUILD, 'zfs-salvage-demo-youtube.mp4')
    subprocess.run([FF, '-loglevel', 'error', '-y', '-i', silent, '-i', wav, '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
                    '-shortest', '-movflags', '+faststart', youtube], check=True)
    gif = os.path.join(BUILD, 'zfs-salvage-demo.gif')
    tmp = os.path.join(BUILD, 'gif.mp4')
    gtotal = render(gif_edit(rec), ['-c:v', 'libx264', '-crf', '12', '-pix_fmt', 'yuv420p', tmp], scale=(800, 450), fps=10)
    subprocess.run([FF, '-loglevel', 'error', '-y', '-i', tmp, '-vf',
                    'split[a][b];[a]palettegen=max_colors=96:stats_mode=diff[p];'
                    '[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle', gif], check=True)
    os.remove(tmp)
    thumbnail(os.path.join(BUILD, 'thumbnail.png'))
    print(f'{silent}: {total:.0f} s, {os.path.getsize(silent) / 1e6:.1f} MB; youtube with music: '
          f'{os.path.getsize(youtube) / 1e6:.1f} MB; {gif}: {gtotal:.0f} s, {os.path.getsize(gif) / 1e6:.1f} MB')


if __name__ == '__main__':
    main()
