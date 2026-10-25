#!/usr/bin/env python3
"""Status page (status/index.html) plus one line per run in logs/status.log.

Pool specifics (target dataset, TXGs, timeline, devices) come from the case file via common.py.
web.py regenerates the page every 20 s; it can also be run on its own.
"""
import glob
import gzip
import html
import json
import math
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (CANDIDATES, CASE, DB, HOST, LOGS, PROBE_STATS, RAW, SCAN_DEVICE, SCAN_LOG, STATUS as OUT,
                    TARGET, TARGET_NAME, TREE_GROUPS, WORKFLOW, profile, ssh, ssh_error)

SMART_CACHE = f'{OUT}/.smart.json'
LOG = SCAN_LOG
L6_LEVEL = (profile().get('meta_dnode_levels') or 7) - 2


def fmt_bytes(n):
    return f'{n / 1e12:.2f} TB' if n >= 1e12 else f'{n / 1e9:.1f} GB' if n >= 1e9 else f'{n / 1e6:.0f} MB'


def num(x):
    return f'{x:,}'


def chunk_counts(path):
    cache = path.replace('.tsv.gz', '.counts.json')
    if os.path.exists(cache):
        return json.load(open(cache))
    c = {'OBJSET': 0, 'IND': 0, 'DNODES': 0, 'L6': 0}
    with gzip.open(path, 'rt') as f:
        for line in f:
            k = line.split('\t', 2)[1]
            c[k] = c.get(k, 0) + 1
            if k == 'IND' and f'\tchild_level={L6_LEVEL}\t' in line:
                c['L6'] += 1
    json.dump(c, open(cache, 'w'))
    return c


def smart():
    try:
        s = json.load(open(SMART_CACHE))
        if time.time() - s['t'] < 300:
            return s
    except Exception:
        pass
    disks = ' '.join(CASE.get('smart_disks', []))
    out = ssh(f"for d in {disks}; do smartctl -A /dev/$d | awk -v d=$d '$1==194{{print d, $10}}'; done") if disks else ''
    s = {'t': time.time(), 'temp': dict(l.split() for l in out.splitlines() if len(l.split()) == 2)}
    json.dump(s, open(SMART_CACHE, 'w'))
    return s


# ---------------------------------------------------------------- TXG -> time
TIMELINE = sorted((e['txg'], e['time']) for e in CASE.get('timeline', []))
if CASE.get('crash_txg'):
    TIMELINE = [u for u in TIMELINE if u[0] <= CASE['crash_txg']]   # ignore later writes by rescue attempts


def txg_time(txg):
    """Time of a TXG from the uberblock timeline: exact, interpolated, or None."""
    for x, t in TIMELINE:
        if x == txg:
            return t, 'exact'
    lo = max((u for u in TIMELINE if u[0] < txg), default=None)
    hi = min((u for u in TIMELINE if u[0] > txg), default=None)
    if lo and hi:
        return lo[1] + (hi[1] - lo[1]) * (txg - lo[0]) / (hi[0] - lo[0]), '≈'
    if TIMELINE and txg < TIMELINE[0][0]:
        return None, 'before ' + time.strftime('%Y-%m-%d', time.localtime(TIMELINE[0][1]))
    return None, ''


def txg_label(txg):
    t, how = txg_time(txg)
    if t is None:
        return f'{num(txg)} ({how})' if how else num(txg)
    return f'{num(txg)} ({"" if how == "exact" else "≈ "}{time.strftime("%Y-%m-%d %H:%M", time.localtime(t))})'


def txg_date(txg):
    """Date first, for headlines: '2014-10-23 03:23' (TXG shown separately)."""
    t, how = txg_time(txg)
    return ('' if how == 'exact' else '≈ ') + time.strftime('%Y-%m-%d %H:%M', time.localtime(t)) if t else f'TXG {num(txg)}'


# ---------------------------------------------------------------- visuals
SEQ = ['#cde2fb', '#9ec5f4', '#6da7ec', '#3987e5', '#256abf', '#184f95', '#0d366b']  # blue 100..700
STATUS = {'good': ('#0ca30c', '✓'), 'warning': ('#fab219', '⟳'), 'serious': ('#ec835a', '!'),
          'critical': ('#d03b3b', '✗'), 'neutral': ('var(--mut)', '·')}


def diagnosis():
    """What make-case found out about where the way to the target's files is broken (case['diagnosis']); cases written by
    hand before that existed carry TARGET['break'] instead."""
    diag = CASE.get('diagnosis')
    brk = TARGET.get('break')
    if diag is None and brk:
        diag = {'situation': 'objset', 'uberblock_txg': CASE.get('crash_txg'), 'mos_txg': CASE.get('open_txg'),
                'dataset_listed': True, 'objset_birth': brk.get('birth_txg'), 'objset_error': brk.get('reason')}
    return diag or {}


def chain_nodes(state):
    """The boxes of the chain uberblock -> ... -> file contents, each with the state the facts support. Nothing is
    drawn as broken unless the diagnosis says so."""
    ds = TARGET.get('dataset', 'target dataset')
    d = diagnosis()
    sit = d.get('situation')
    ub = d.get('uberblock_txg') or CASE.get('crash_txg')
    ub_sub = (f'TXG {ub} · ' + ('last state before the crash' if CASE.get('crash_txg') else 'newest uberblock')) if ub else '–'
    mos = d.get('mos_txg') or CASE.get('open_txg')
    if sit == 'mos':
        mos_node = ('Pool metadata (MOS)', 'unreadable at every uberblock', 'critical', 'Meta Object Set: directory of all datasets. This is where the chain breaks')
    else:
        mos_node = ('Pool metadata (MOS)', f'readable at TXG {mos}' if mos else '–', 'good' if mos else 'neutral',
                    'Meta Object Set: directory of all datasets')
    info = ' · '.join(x for x in (f'MOS obj {TARGET["mos_dir_obj"]}/{TARGET["mos_dataset_obj"]}' if TARGET.get('mos_dir_obj') else '',
                                  TARGET.get('size_hint', ''),
                                  f'{num(TARGET["expected_objects"])} objects' if TARGET.get('expected_objects') else '') if x)
    if sit == 'mos':
        ds_node = (f'Dataset {ds}', '–', 'neutral', 'Dataset entry with size, snapshots and the pointer to its objset block')
    elif sit == 'missing':
        ds_node = (f'Dataset {ds}', "not in the pool's list – destroyed, or the name is wrong", 'critical',
                   'Dataset entry with size, snapshots and the pointer to its objset block. This is where the chain breaks')
    else:
        ds_node = (f'Dataset {ds}', info or ('listed' if d.get('dataset_listed') else '–'), 'good' if d else 'neutral',
                   'Dataset entry with size, snapshots and the pointer to its objset block')
    if sit == 'objset':
        os_node = ('Objset block', f'TXG {d.get("objset_birth") or "?"} · {d.get("objset_error") or "unreadable"}', 'critical',
                   'This is where the chain breaks')
    elif sit == 'intact':
        os_node = ('Objset block', f'TXG {d.get("objset_birth") or "?"} · readable', 'good', 'The dataset opens normally')
    else:
        os_node = ('Objset block', '–', 'neutral', 'Root of the dataset: the first block of its file tree')
    return [
        ('Uberblock', ub_sub, 'good' if ub else 'neutral', 'Entry point of the pool; ring buffer of the last 128 states'),
        mos_node, ds_node, os_node,
        (f'Dnode tree L{L6_LEVEL + 1} → L1', state['tree'][1], state['tree'][0],
         'Pointer tree to all file entries – the scanner finds it directly on disk'),
        ('File entries (dnodes)', state['dnodes'][1], state['dnodes'][0],
         'Name, size, date, pointers to contents; 32 per 16 KiB block'),
        ('File contents', state['files'][1], state['files'][0],
         'Blocks with their own checksums – every block is verified individually'),
    ]


def chain_break(nodes):
    """Index of the first broken box before the dnode tree, or None."""
    return next((i for i, n in enumerate(nodes[:4]) if n[2] == 'critical'), None)


def chain_svg(state):
    """ZFS chain uberblock -> ... -> file contents with states; the scanner's detour is drawn around a real break only."""
    nodes = chain_nodes(state)
    brk = chain_break(nodes)
    w, nh, gap, x = 560, 54, 22, 20
    h = len(nodes) * (nh + gap)
    out = [f'<svg viewBox="0 0 {w + 200} {h}" role="img" aria-label="ZFS chain from uberblock to files" '
           f'style="width:100%;max-width:{w + 200}px;height:auto">',
           '<defs><marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
           '<path d="M0,0 L10,5 L0,10 z" fill="var(--mut)"/></marker></defs>']
    for i, (title, sub, st, tip) in enumerate(nodes):
        y = i * (nh + gap)
        col, icon = STATUS[st]
        out.append(f'<g><title>{html.escape(tip)}</title>'
                   f'<rect x="{x}" y="{y}" width="{w}" height="{nh}" rx="8" fill="var(--card)" stroke="var(--line)"/>'
                   f'<rect x="{x}" y="{y}" width="6" height="{nh}" rx="3" fill="{col}"/>'
                   f'<circle cx="{x + 28}" cy="{y + nh / 2}" r="11" fill="{col}"/>'
                   f'<text x="{x + 28}" y="{y + nh / 2 + 5}" text-anchor="middle" font-size="14" fill="#fff" font-weight="700">{icon}</text>'
                   f'<text x="{x + 50}" y="{y + 22}" font-size="14" font-weight="600" fill="var(--fg)">{html.escape(title)}</text>'
                   f'<text x="{x + 50}" y="{y + 41}" font-size="12" fill="var(--mut)">{html.escape(sub)}</text></g>')
        if i < len(nodes) - 1:
            dash = ' stroke-dasharray="4 4"' if st == 'critical' or nodes[i + 1][2] == 'critical' else ''
            out.append(f'<line x1="{x + 28}" y1="{y + nh}" x2="{x + 28}" y2="{y + nh + gap - 2}" '
                       f'stroke="var(--mut)" stroke-width="2"{dash} marker-end="url(#ar)"/>')
    if brk is None:
        out.append('</svg>')
        return ''.join(out)
    y2, y4, xr = max(brk - 1, 0) * (nh + gap) + nh / 2, 4 * (nh + gap) + nh / 2, x + w + 30
    out.append(f'<path d="M{x + w},{y2} H{xr} V{y4} H{x + w + 4}" fill="none" stroke="{SEQ[4]}" stroke-width="2" '
               f'stroke-dasharray="6 4" marker-end="url(#ar)"/>'
               f'<text x="{xr + 8}" y="{(y2 + y4) / 2 - 6}" font-size="12" fill="var(--fg)" font-weight="600">Detour</text>'
               f'<text x="{xr + 8}" y="{(y2 + y4) / 2 + 10}" font-size="12" fill="var(--mut)">the scanner finds</text>'
               f'<text x="{xr + 8}" y="{(y2 + y4) / 2 + 25}" font-size="12" fill="var(--mut)">the tree directly</text>'
               f'<text x="{xr + 8}" y="{(y2 + y4) / 2 + 40}" font-size="12" fill="var(--mut)">on disk</text></svg>')
    return ''.join(out)


def disk_map(n, step, done_counts, cur_idx, cur_frac):
    """One tile per chunk; colour = amount of metadata found (log scale); running chunk = fill level."""
    vals = [sum(c.values()) - c.get('L6', 0) for c in done_counts.values()] or [1]
    vmax = max(vals) or 1
    cells = []
    for i in range(n):
        lo, hi = i * step / 1e9, (i + 1) * step / 1e9
        if i in done_counts:
            c = done_counts[i]
            tot = sum(c.values()) - c.get('L6', 0)
            k = 0 if tot == 0 else min(len(SEQ) - 1, int(math.log1p(tot) / math.log1p(vmax) * (len(SEQ) - 1) + 0.5))
            tip = (f'{lo:.0f}–{hi:.0f} GB · scanned · objsets {num(c["OBJSET"])} · roots {num(c["L6"])} · '
                   f'indirect blocks {num(c["IND"])} · dnode blocks {num(c["DNODES"])}')
            cells.append(f'<div class="cell" style="background:{SEQ[k]}" title="{html.escape(tip)}"></div>')
        elif i == cur_idx:
            cells.append(f'<div class="cell act" title="{lo:.0f}–{hi:.0f} GB · reading now ({100 * cur_frac:.0f} %)">'
                         f'<div style="width:{100 * cur_frac:.0f}%"></div></div>')
        else:
            cells.append(f'<div class="cell todo" title="{lo:.0f}–{hi:.0f} GB · not read yet"></div>')
    legend = ''.join(f'<span class="sw" style="background:{s}"></span>' for s in SEQ)
    return (f'<div class="grid">{"".join(cells)}</div><div class="legend"><span>few finds</span>{legend}'
            f'<span>many finds</span><span class="sw act-sw"></span><span>reading</span>'
            f'<span class="sw todo"></span><span>pending</span></div>')


def chain_state(cands, scanning):
    ok = [h for h in cands if 'verified' in str(h.get('status', ''))]
    if ok:
        best = max(ok, key=lambda h: int(h.get('birth') or 0))
        return {'tree': ('good', f'found · TXG {best.get("birth")} · {best.get("kind")} @ {best.get("offset")}'),
                'dnodes': ('good', f'{num(best.get("objects") or 0)} entries reachable'),
                'files': ('warning', 'contents are verified during extraction')}
    if scanning:
        return {'tree': ('warning', f'searching · {len(cands)} candidates verified'),
                'dnodes': ('neutral', 'reachable once the tree is found'),
                'files': ('neutral', 'not checked yet')}
    return {'tree': ('serious', 'scan finished, no matching tree yet'),
            'dnodes': ('neutral', '–'), 'files': ('neutral', '–')}


# ---------------------------------------------------------------- page
def groups_card(groups, needs_choice):
    """Which file system to recover: shown while the choice is open, and when more than one was found."""
    if not groups or (len(groups) < 2 and not needs_choice):
        return ''
    rows = ''
    for g in groups:
        top = ', '.join(g['top']) + (' …' if len(g['top']) >= 12 else '')
        rows += (f'<tr><td><b>{html.escape(top)}</b><br><span class="mut">{num(g["objects"] or 0)} objects in the newest state · '
                 f'{g["trees"]} state(s), {txg_label(g["oldest_txg"])} – {txg_label(g["newest_txg"])}</span></td>'
                 f'<td style="text-align:right">'
                 + ('<span class="mut">✓ chosen</span>' if g['chosen'] else
                    f'<button class="pri" data-names="{html.escape(json.dumps(g["names"]), quote=True)}" '
                    f'onclick="choose(this)">This one</button>') + '</td></tr>')
    title = ('Found %d different file systems – which one do you want to recover?' % len(groups) if needs_choice
             else 'Found %d different file systems' % len(groups))
    return f'<div style="margin-top:12px;border-top:1px solid var(--line);padding-top:10px"><b>{title}</b><table style="width:100%">{rows}</table></div>'


def states_card(cands, cat_state):
    """All verified states of the chosen file system, newest first: the catalog can be built from any of them, e.g.
    from before something was deleted. Empty when there is only one."""
    usable = [c for c in cands if c.get('candidate')]
    if len(usable) < 2:
        return ''
    rows = ''
    for c in usable:
        cand = dict(c['candidate'], txg=c['birth'])
        action = ('<span class="mut">✓ in the catalog</span>' if cat_state == c['birth'] else
                  f'<button data-cand="{html.escape(json.dumps(cand), quote=True)}" onclick="rebuildFrom(this)">Build catalog from this state</button>')
        rows += (f'<tr><td>{html.escape(txg_date(c["birth"]))}</td><td>{num(c["birth"])}</td><td>{num(c.get("objects") or 0)}</td>'
                 f'<td class="mut">{html.escape(str(c.get("root")))}</td><td>{action}</td></tr>')
    return (f'<details class="card"><summary>{len(usable)} recoverable states – choose another one, e.g. from before something was deleted</summary>'
            '<p class="mut">Every state is a complete, checksum-verified picture of the file system at that time. The longer the pool '
            'was used after a deletion, the more of the old blocks have been overwritten: files from an older state can come out '
            'incomplete, and the transfer lists every file that does.</p>'
            f'<div class="wrap" style="max-height:340px;overflow-y:auto"><table><tr><th>Date</th><th>TXG</th><th>Objects</th>'
            f'<th>Top level</th><th></th></tr>{rows}</table></div></details>')


def main():
    os.makedirs(OUT, exist_ok=True)
    log = open(LOG).read() if os.path.exists(LOG) else ''
    m = re.search(r'(\d+) bytes, (\d+) (?:Abschnitte à|chunks of) (\d+) GiB', log, re.I)       # older logs are German
    size, n, gib = (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 64)
    step = gib << 30
    done = sorted(glob.glob(f'{RAW}/chunk_*.tsv.gz.done'))
    durs = [int(x) for x in re.findall(r'(?:Funde|finds), (\d+) s', log)]
    totals = {'OBJSET': 0, 'IND': 0, 'DNODES': 0, 'L6': 0}
    rows, done_counts = [], {}
    for d in done:
        f = d[:-5]
        c = chunk_counts(f)
        for k in totals:
            totals[k] += c.get(k, 0)
        rows.append((os.path.basename(f), c))
        done_counts[int(re.search(r'chunk_(\d+)', f).group(1))] = c

    cur = ssh("p=$(pgrep -x zscan | head -1); [ -n \"$p\" ] && echo $(tr '\\0' ' ' < /proc/$p/cmdline) "
              "$(awk '/^rchar/{print $2}' /proc/$p/io) $(ps -o etimes= -p $p)")
    cur_txt, cur_frac, cur_idx = 'no zscan process running', 0.0, -1
    parts = cur.split()
    if len(parts) >= 6:
        start, end, rchar, et = int(parts[2]), int(parts[3]), int(parts[4]), int(parts[5])
        cur_idx = start // step
        cur_frac = min(rchar / max(end - start, 1), 1.0)
        cur_txt = (f'chunk {cur_idx} ({start / 1e9:.0f}–{end / 1e9:.0f} GB): {100 * cur_frac:.1f} %, '
                   f'{rchar / max(et, 1) / 1e6:.0f} MB/s, running {et // 60} min')
    frac = (len(done) + cur_frac) / n if n else 0
    avg = sum(durs) / len(durs) if durs else None
    eta = (n - len(done) - cur_frac) * avg if avg else None
    temps = ', '.join(f'{d}: {t} °C' for d, t in sorted(smart().get('temp', {}).items())) or '–'

    host_error = ssh_error()
    cands = []
    try:
        cands = json.load(open(CANDIDATES))
    except Exception:
        pass
    pstats = {}
    try:
        pstats = json.load(open(PROBE_STATS))
    except Exception:
        pass
    byds = pstats.get('by_dataset', {})
    try:
        tg = json.load(open(TREE_GROUPS))
    except Exception:
        tg = {}
    groups, needs_choice = tg.get('groups', []), tg.get('needs_choice', False)
    host_error = host_error or pstats.get('host_error')
    alerts = []                                       # (severity, text): problems the user must see
    if host_error:
        alerts.append(('error', f'Recovery host {HOST} is unreachable: {host_error} '
                       '(set ZFS_SALVAGE_HOST, check the SSH key and that the host is running)'))
    scan_err = [l for l in log.splitlines()[-3:] if l.startswith('ERROR:')]
    if scan_err:
        alerts.append(('error', 'Scan: ' + scan_err[-1][6:].strip()))
    hidden = pstats.get('hidden_newer') or {}
    if hidden.get('count'):
        alerts.append(('warn', f'{hidden["count"]} newer state(s) of this file system, up to {txg_label(hidden["newest_birth"])}, could not be '
                       'read: damage inside the file tree hides them. The best readable state is older, so changes made after it '
                       f'({txg_label(hidden["best_birth"])}) are missing from the catalog.'))
    if pstats.get('slow_mode'):
        alerts.append(('warn', 'Slow mode: the C library is not used on the recovery host, reading falls back to '
                       'pure Python. ' + (pstats.get('slow_reason') or '').replace('zsread: SLOW MODE - ', '')))
    probe_txt = (f'{num(pstats.get("probed", 0))} candidates verified, {num(pstats.get("pending", 0))} pending'
                 + (f', {num(pstats["deferred"])} older ones deferred until the scan is done' if pstats.get('deferred') else '')
                 + ' · ' + ' · '.join(f'{k}: {num(v)}' for k, v in sorted(byds.items(), key=lambda kv: -kv[1])))
    newest = cands[0]['birth'] if cands else None
    goal = TARGET.get('last_change_txg')
    gap_txt = '–'
    if newest and goal:
        if newest >= goal:
            gap_txt = 'reached ✓'
        else:
            a, _ = txg_time(newest)
            b, _ = txg_time(goal)
            gap_txt = f'{num(goal - newest)} TXGs' + (f' · {(b - a) / 3600:.1f} h' if a and b else '')

    cat_state = None
    try:
        cat_state = int(sqlite3.connect(f'file:{DB}?mode=ro', uri=True).execute(
            "select state from entry where path='/'").fetchone()[0])
    except Exception:
        pass
    best = cands[0] if cands else {}
    note = '<span class="mut">your selection is kept</span>'
    if not newest and needs_choice:
        action = '<span class="mut">Found several file systems – tell which one to recover (below).</span>'
    elif not newest:
        action = '<span class="mut">No verified state yet – candidates are checked as soon as chunks are done.</span>'
    elif cat_state is None:
        action = f'<button class="pri" onclick="rebuild()">Build catalog from this state</button>'
    elif newest > cat_state:
        action = (f'<button class="pri" onclick="rebuild()">Update catalog to this state</button> {note}'
                  f'<div class="mut" style="margin-top:6px">The catalog still shows {txg_label(cat_state)} · '
                  f'<a href="/select">continue with it →</a></div>')
    else:
        action = '<a class="btn pri" href="/select">Choose files to recover →</a>'
    hero = (f'<div class="hero"><div><span class="mut">Best recoverable state of {html.escape(TARGET.get("dataset", "target"))}</span>'
            f'<b class="big">{txg_date(newest) if newest else "–"}</b>'
            f'<span class="mut">TXG {num(newest or 0)} · {num(best.get("objects") or 0)} objects · top level: {html.escape(str(best.get("root") or "–"))}</span></div>'
            '<div class="kpi">' + (f'<div>Goal: last change<b>{txg_label(goal)}</b></div><div>Distance to goal<b>{gap_txt}</b></div>'
                                   if goal else '')          # only cases that know when the dataset last changed
            + f'<div>Readable states<b>{num(byds.get(TARGET_NAME, 0))}</b></div></div>'
            f'<div>{action} <span id="cj" class="mut"></span></div></div>')
    scanning = cur_idx >= 0 or len(done) < n
    banner = ''.join(f'<div class="alert {sev}"><b>{"✗" if sev == "error" else "!"}</b> {html.escape(text)}</div>'
                     for sev, text in alerts)
    hero = banner + hero + groups_card(groups, needs_choice)
    json.dump({'alerts': [t for _, t in alerts], 'scan': {'running': cur_idx >= 0, 'pct': 100 * frac, 'eta': eta},
               'newest': {'txg': newest, 'label': txg_label(newest) if newest else None,
                          'date': txg_date(newest) if newest else None,
                          'objects': best.get('objects')}, 'goal': goal},
              open(WORKFLOW + '.tmp', 'w'))
    os.replace(WORKFLOW + '.tmp', WORKFLOW)

    now = time.strftime('%Y-%m-%d %H:%M:%S')
    line = (f'{now}  {100 * frac:5.1f} %  chunks {len(done)}/{n}  OBJSET {totals["OBJSET"]}  L6 {totals["L6"]}  '
            f'IND {totals["IND"]}  DNODES {totals["DNODES"]}  ETA {eta / 3600:.1f} h  {temps}' if eta else
            f'{now}  {100 * frac:5.1f} %  {cur_txt}  {temps}')
    os.makedirs(LOGS, exist_ok=True)
    with open(f'{LOGS}/status.log', 'a') as f:
        f.write(line + '\n')

    ds = html.escape(TARGET.get('dataset', 'target'))
    eta_txt = (time.strftime('%H:%M', time.localtime(time.time() + eta)) + f' (in {eta / 3600:.1f} h)'
               if eta else 'after the first chunk')
    trs = '\n'.join(f'<tr><td>{html.escape(nm)}</td><td>{num(c["OBJSET"])}</td><td>{num(c["L6"])}</td>'
                    f'<td>{num(c["IND"])}</td><td>{num(c["DNODES"])}</td></tr>' for nm, c in rows[-12:][::-1])
    crs = '\n'.join(
        f'<tr><td>{html.escape(str(h.get("offset")))}</td><td>{html.escape(str(h.get("kind")))}</td>'
        f'<td>{html.escape(txg_label(int(h.get("birth") or 0)))}</td><td>{num(h.get("objects") or 0)}</td>'
        f'<td>{html.escape(str(h.get("root")))}</td><td>{html.escape(str(h.get("status")))}</td></tr>'
        for h in cands) or '<tr><td colspan="6">none yet – verification starts as soon as chunks are done</td></tr>'
    crash_note = ''
    if goal and CASE.get('crash_txg'):
        crash_note = (f'<p class="mut" style="margin:-6px 4px 0">After its last change ({txg_label(goal)}) the dataset '
                      f'was not modified until the crash ({txg_label(CASE["crash_txg"])}). A TXG is a pool-wide '
                      f'transaction (~every 5 s while anything is written), not every TXG touches the dataset.</p>')
    broken = chain_break(chain_nodes(chain_state(cands, scanning))) is not None
    chain_title = ' – and where the chain breaks' if broken else ''
    chain_text = ('The scanner bypasses the broken step and finds the pointer tree directly on disk.' if broken else
                  'Nothing on the way to your files is known to be broken; the scan also finds the older states of the file system.'
                  if diagnosis() else 'The scanner finds the pointer tree directly on disk, whatever is broken above it.')
    scan_head = (f'<b>{100 * frac:.1f} %</b> · {len(done)} of {n} chunks · expected to finish {eta_txt}' if scanning
                 else f'<b>Scan complete</b> · {n} chunks')
    page = f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>zfs-salvage status</title>
<style>
:root{{--bg:#f7f7f5;--fg:#1d1d1b;--mut:#6b6b66;--card:#fff;--line:#e2e2dc;--acc:#2f6f4f;--bar:#d9e8df;--bad:#d03b3b}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151614;--fg:#e8e8e3;--mut:#9a9a93;--card:#1e1f1c;--line:#33342f;--acc:#7fc29b;--bar:#26372d;--bad:#e66767}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}}
main{{max-width:960px;margin:0 auto;padding:16px}} a{{color:var(--acc)}}
h1{{font-size:20px;margin:0 0 2px}} .mut{{color:var(--mut);font-size:13px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:12px 0}}
.hero{{display:grid;gap:12px}} .hero .big{{display:block;font-size:24px;margin:2px 0}}
button,.btn{{font:inherit;font-size:14px;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:8px;padding:8px 14px;cursor:pointer;text-decoration:none;display:inline-block}}
.pri{{background:var(--acc);border-color:var(--acc);color:#fff}} @media (prefers-color-scheme:dark){{.pri{{color:#10140f}}}}
.bar{{height:14px;background:var(--bar);border-radius:7px;overflow:hidden}} .bar>div{{height:100%;background:var(--acc)}}
.kpi{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}}
.kpi div{{border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:13px;color:var(--mut)}}
.kpi b{{font-size:17px;display:block;color:var(--fg);font-variant-numeric:tabular-nums}}
.grid{{display:grid;grid-template-columns:repeat(11,1fr);gap:2px;margin:8px 0}}
.cell{{aspect-ratio:1.6;border-radius:4px;position:relative;overflow:hidden}}
.cell.todo,.sw.todo{{background:transparent;border:1px solid var(--line)}}
.cell.act{{border:2px solid var(--acc);background:transparent}} .cell.act>div{{height:100%;background:var(--bar)}}
.legend{{display:flex;flex-wrap:wrap;align-items:center;gap:4px 6px;font-size:12px;color:var(--mut)}}
.sw{{display:inline-block;width:16px;height:12px;border-radius:3px}} .act-sw{{border:2px solid var(--acc);margin-left:10px}}
details.card>summary{{cursor:pointer;font-weight:600}} details summary{{cursor:pointer;color:var(--mut);font-size:13px}}
table{{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}}
td,th{{text-align:left;padding:4px 6px;border-bottom:1px solid var(--line)}} .wrap{{overflow-x:auto}}
h3{{font-size:14px;margin:14px 0 6px}}
.alert{{border:1px solid var(--bad);border-left-width:5px;border-radius:8px;padding:8px 12px;margin-bottom:10px;font-size:14px}}
.alert.warn{{border-color:#fab219}} .alert b{{margin-right:4px}}
</style></head><body><main>
<nav id="steps" data-page="status"></nav>
<h1>zfs-salvage · {ds}</h1>
<div class="mut" id="upd">Updated {now} · refreshes every 20 s · case {html.escape(CASE.get("name", "–"))}</div>
<div class="card" id="hero">{hero}</div>
<div id="states">{states_card(cands, cat_state)}</div>
{crash_note}
<div class="card" id="scan"><b>Scan</b> <span class="mut">– device {html.escape(SCAN_DEVICE)} ({fmt_bytes(size)})</span>
<div class="bar" style="margin:8px 0"><div style="width:{100 * frac:.1f}%"></div></div>
<div>{scan_head}</div><div class="mut">{html.escape(cur_txt)} · disk temperature {temps}</div>
<details style="margin-top:8px"><summary>Disk map – one tile per {gib} GiB, colour = amount of ZFS metadata found</summary>
{disk_map(n, step, done_counts, cur_idx, cur_frac)}</details></div>
<div class="card" id="chain"><b>How ZFS finds your files{chain_title}</b>
<p class="mut">Every step points to the next one and carries its checksum. {chain_text} Hover over a box for an explanation.</p>
{chain_svg(chain_state(cands, scanning))}
<p class="mut">✓ intact and proven by checksum · ✗ destroyed · ⟳ being searched · · not reached yet. TXG = transaction group number, higher is newer.</p></div>
<details class="card" id="details"><summary>Scan details</summary><div id="det">
<div class="kpi" style="margin-top:10px"><div>Objset blocks<b>{num(totals["OBJSET"])}</b></div><div>Tree roots<b>{num(totals["L6"])}</b></div>
<div>Indirect blocks<b>{num(totals["IND"])}</b></div><div>Dnode blocks<b>{num(totals["DNODES"])}</b></div></div>
<h3>Candidates for {ds}</h3><p class="mut">{html.escape(probe_txt)} · newest first, consecutive identical states collapsed</p>
<div class="wrap"><table><tr><th>Offset</th><th>Kind</th><th>TXG (time)</th><th>Objects</th><th>Top level</th><th>Status</th></tr>{crs}</table></div>
<h3>Latest chunks</h3><div class="wrap"><table><tr><th>Chunk</th><th>Objsets</th><th>Roots</th><th>Indirect</th><th>Dnodes</th></tr>{trs}</table></div>
</div></details>
</main>
<script src="/steps.js"></script>
<script>
// live refresh: swap the generated parts, keep open/closed state of <details>
setInterval(async()=>{{try{{const d=new DOMParser().parseFromString(await (await fetch('/')).text(),'text/html');
 for(const id of ['upd','hero','det'])document.getElementById(id).innerHTML=d.getElementById(id).innerHTML;
 for(const id of ['scan','chain','states']){{const o=document.getElementById(id),open=[...o.querySelectorAll('details')].map(x=>x.open);
  o.innerHTML=d.getElementById(id).innerHTML;o.querySelectorAll('details').forEach((x,i)=>x.open=!!open[i])}}
 cj()}}catch(e){{}}}},20000);
let poll;
async function cj(){{try{{const j=await (await fetch('/api/catalog/status')).json(),e=document.getElementById('cj');
 if(!e||j.state==='idle')return;
 e.textContent=j.state==='running'?`⟳ building catalog from TXG ${{j.txg}} – ${{j.info}} …`:j.state==='failed'?`✗ failed: ${{j.info}}`:'';
 if(j.state==='running'){{document.querySelectorAll('#hero button, #states button').forEach(b=>b.disabled=true);poll=poll||setInterval(cj,3000)}}
 else if(poll){{clearInterval(poll);poll=null;if(j.state==='done')location.reload()}}}}catch(e){{}}}}
async function choose(b){{b.disabled=true;b.textContent='…';await fetch('/api/target/choose',{{method:'POST',body:JSON.stringify({{names:JSON.parse(b.dataset.names)}})}});location.reload()}}
async function rebuildFrom(b){{b.disabled=true;await fetch('/api/catalog/rebuild',{{method:'POST',body:JSON.stringify({{candidate:JSON.parse(b.dataset.cand)}})}});cj()}}
async function rebuild(){{await fetch('/api/catalog/rebuild',{{method:'POST',body:'{{}}'}});cj()}}
cj();
</script></body></html>'''
    tmp = f'{OUT}/index.html.tmp'
    open(tmp, 'w').write(page)
    os.replace(tmp, f'{OUT}/index.html')


if __name__ == '__main__':
    main()
