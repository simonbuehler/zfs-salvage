#!/usr/bin/env python3
"""Probe the tree-root candidates from finished scan chunks on the recovery host
(zsread.py batch) and rank the verified states of the target dataset.

- Candidates: tree-root blocks (IND whose entries are on level levels - 2, e.g. L6 blocks of a 7-level
  tree) plus objset blocks whose tree root was not found itself. Identical content (same checksum) is probed only once.
- Order: newest TXG first. Results are appended to status/probed.jsonl.
- A candidate is a state of the target if its root directory contains one of the
  case's target.root_names.
- Output: status/target_candidates.json (newest states, for the status page and the
  catalog) and status/probe_stats.json.

Usage: rank-trees.py [--loop] [max_per_run=1500]
  --loop   repeat every 3 minutes until the scan is done and nothing is pending
           (e.g. scripts/rank-trees.py --loop >> logs/rank-trees.log &)
"""
import glob
import gzip
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (CANDIDATES, PROBE_STATS, PROBED, RAW, RAWS, TARGET, TARGET_NAME, TREE_PROFILE,
                    classify, newest_txg, profile, reader_cmd, remote_argv, ssh, ssh_error)

LABEL = profile().get('label_start', 0x400000)


def infer_levels(raws):
    """Height of the meta-dnode tree of file systems, from the scan itself: the most common `nlevels` of
    the ZPL objset blocks found (the MOS and zvols are not counted, zscan only reports ZPL objsets).
    None if no objset block was found yet."""
    counts = {}
    for raw in raws if isinstance(raws, (list, tuple)) else [raws]:
        for done in glob.glob(f'{raw}/chunk_*.tsv.gz.done'):
            with gzip.open(done[:-5], 'rt') as f:
                for line in f:
                    c = line.split('\t', 7)
                    if len(c) > 6 and c[1] == 'OBJSET' and 'os_type=3' not in c[6]:      # not a zvol
                        n = next((x.split('=')[1] for x in c[6].split('\t') if x.startswith('nlevels=')), None)
                        if n:
                            counts[int(n)] = counts.get(int(n), 0) + 1
    return max(counts, key=counts.get) if counts else None


def candidates(raw=RAW, levels=None, label=LABEL, seen=None):
    """Tree-root candidates from the finished scan chunks in raw (parameters: tests/run.py).

    A file `vdev` in raw names the top-level vdev of the scanned device; offsets of other vdevs than 0
    are written as '<vdev>:<hex>', which zsread.py understands. `seen` (checksums) can be shared
    between several scan directories so that identical blocks are probed only once.
    """
    roots, objsets = {}, []
    seen = set() if seen is None else seen
    levels = levels or infer_levels(raw)
    if not levels:
        return []
    vdev = 0
    if os.path.exists(os.path.join(raw, 'vdev')):
        with open(os.path.join(raw, 'vdev')) as f:
            vdev = int(f.read().strip() or 0)
    pre = f'{vdev}:' if vdev else ''
    for done in sorted(glob.glob(f'{raw}/chunk_*.tsv.gz.done')):
        with gzip.open(done[:-5], 'rt') as f:
            for line in f:
                c = line.rstrip('\n').split('\t')
                kv = dict(x.split('=', 1) for x in c[6:] if '=' in x)
                if c[1] == 'IND' and kv.get('child_level') == str(levels - 2):
                    if c[5] in seen:
                        continue
                    seen.add(c[5])
                    roots[pre + c[0]] = ('IND', pre + c[0], c[2], c[3], c[4], int(kv['max_birth']))
                elif c[1] == 'OBJSET' and kv.get('nlevels') == str(levels):
                    objsets.append(('OBJSET', pre + c[0], c[2], c[3], c[4], int(kv['bp_birth']),
                                    f'{pre}{int(kv["bp_dva0"], 16) + label:x}', c[5]))
    out = list(roots.values())
    for o in objsets:
        if o[6] not in roots and o[7] not in seen:
            seen.add(o[7])
            out.append(o[:6])
    return out


def min_birth_during_scan():
    """While the scan is still reading the disk, only candidates born at or after this TXG are probed.

    Random reads of old states would slow the sequential scan down; the older ones follow once the scan
    is done. The threshold is the newest TXG of the case minus 20 % of the pool's TXG history (TXGs count
    from the creation of the pool, so this scales with the pool's age). MIN_BIRTH_DURING_SCAN overrides it;
    a case without a timeline defers nothing.
    """
    if os.environ.get('MIN_BIRTH_DURING_SCAN'):
        return int(os.environ['MIN_BIRTH_DURING_SCAN'])
    newest = newest_txg()
    return newest - newest // 5 if newest else 0


def run(limit):
    os.makedirs(os.path.dirname(PROBED), exist_ok=True)          # a fresh installation has no status directory yet
    probed = {}
    if os.path.exists(PROBED):
        for line in open(PROBED):
            r = json.loads(line)
            probed[r['offset']] = r
    seen = set()
    levels = profile().get('meta_dnode_levels') or infer_levels(RAWS)       # one value for all vdevs
    allc = [c for raw in RAWS for c in candidates(raw, levels, seen=seen)] if levels else []
    by_off = {c[1]: c for c in allc}
    todo = [c for c in allc if c[1] not in probed]
    scan_running = ssh('pgrep -x zscan').strip() != ''
    min_birth = min_birth_during_scan() if scan_running else 0
    deferred = sum(1 for c in todo if c[5] < min_birth)
    todo = [c for c in todo if c[5] >= min_birth]
    todo.sort(key=lambda c: -c[5])
    batch = todo[:limit]
    prev = {}
    if os.path.exists(PROBE_STATS):
        with open(PROBE_STATS) as f:
            prev = json.load(f)
    health = {'host_error': ssh_error(), 'slow_mode': prev.get('slow_mode'), 'slow_reason': prev.get('slow_reason')}
    if batch:
        inp = ''.join(f'{k} {o} {cp} {ps} {ls} {b}\n' for k, o, cp, ps, ls, b in batch)
        res = subprocess.run(remote_argv(reader_cmd('batch'), opts=('-o', 'ServerAliveInterval=30')),
                             input=inp, capture_output=True, text=True, timeout=7200)
        if res.returncode == 255:                # ssh itself failed, nothing was probed
            health['host_error'] = next((l.strip() for l in res.stderr.splitlines() if l.strip()), 'ssh failed')
        slow = [l for l in res.stderr.splitlines() if 'SLOW MODE' in l]
        with open(PROBED, 'a') as f:
            for line in res.stdout.splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                probed[r['offset']] = r
                f.write(line + '\n')
        if res.stdout.strip():
            health['slow_mode'], health['slow_reason'] = bool(slow), (slow[0].strip() if slow else None)

    zvol_target = TARGET.get('type') == 'zvol'

    def label(r):
        if not r.get('ok'):
            return 'unreadable'
        if r.get('type') == 'zvol':                 # a block device: no directory names to recognise it by
            want = TARGET.get('volsize')
            return TARGET_NAME if zvol_target and (not want or r.get('volsize') == want) else 'zvol'
        return 'file system' if zvol_target else classify(r.get('root'))

    states = sorted((r for r in probed.values() if label(r) == TARGET_NAME), key=lambda r: -r['birth'])
    by_dataset = {}
    for r in probed.values():
        k = label(r)
        by_dataset[k] = by_dataset.get(k, 0) + 1
    table, last = [], None
    for r in states:
        sig = (r.get('objects'), tuple(r.get('root', [])))
        if sig == last:
            continue                     # collapse consecutive identical states
        last = sig
        c = by_off.get(r['offset'])
        table.append({'offset': r['offset'], 'kind': f'L{levels - 1}' if r['kind'] == 'IND' else r['kind'],
                      'birth': r['birth'], 'objects': r.get('objects'),
                      'root': (f"zvol, {r['volsize'] / 2**30:.1f} GiB" if r.get('type') == 'zvol' else
                               ', '.join(r.get('root', [])[:12]) + (' …' if len(r.get('root', [])) > 12 else '')),
                      'status': 'verified ✓',
                      # how to read this candidate (for catalog rebuild / extraction)
                      'candidate': {'kind': r['kind'], 'offset': r['offset'], 'comp': c[2], 'psize': int(c[3]),
                                    'lsize': int(c[4])} if c else None})
        if len(table) >= 15:
            break
    root = next((t for t in table if t['kind'].startswith('L') and t['candidate']), None)
    if root:                                   # measured on a verified tree of the target: the true layout
        c = root['candidate']
        measured = {'meta_dnode_levels': levels, 'indirect_block_size': c['lsize'], 'metadata_compression': c['comp'],
                    'measured_on': {'offset': root['offset'], 'txg': root['birth']}}
        os.makedirs(os.path.dirname(TREE_PROFILE), exist_ok=True)
        with open(TREE_PROFILE, 'w') as f:
            json.dump(measured, f)
    os.makedirs(os.path.dirname(CANDIDATES), exist_ok=True)
    json.dump(table, open(CANDIDATES + '.tmp', 'w'), ensure_ascii=False)
    os.replace(CANDIDATES + '.tmp', CANDIDATES)
    pending = max(len(todo) - len(batch), 0)
    with open(PROBE_STATS, 'w') as f:
        json.dump({'probed': len(probed), 'pending': pending, 'deferred': deferred, 'target': TARGET_NAME,
                   'target_states': len(states), 'by_dataset': by_dataset, **health}, f, ensure_ascii=False)
    print(f'probed {len(batch)} new, total {len(probed)}, pending {pending}, deferred {deferred}, '
          f'{TARGET_NAME} states {len(states)}, newest {states[0]["birth"] if states else "-"}; {by_dataset}', flush=True)
    return scan_running or pending or deferred


def main():
    args = [a for a in sys.argv[1:] if a != '--loop']
    limit = int(args[0]) if args else 1500
    if '--loop' not in sys.argv:
        return run(limit)
    while True:
        print(time.strftime('%H:%M '), end='')
        try:
            more = run(limit)
        except Exception as e:  # noqa: BLE001 - keep looping, the recovery host may be rebooting
            print(f'error: {type(e).__name__}: {e}', flush=True)
            more = True
        if not more:
            print('nothing pending – done', flush=True)
            return
        time.sleep(180)


if __name__ == '__main__':
    main()
