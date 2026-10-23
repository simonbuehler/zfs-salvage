#!/usr/bin/env python3
"""Probe the tree-root candidates from finished scan chunks on the recovery host
(zsread.py batch) and rank the verified states of the target dataset.

- Candidates: L6 blocks (IND child_level=5) plus objset blocks whose L6 block was
  not found itself. Identical content (same checksum) is probed only once.
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
from common import (CANDIDATES, CASE, HOST, PROBE_STATS, PROBED, RAW, TARGET_NAME,
                    classify, reader_cmd, ssh)

LABEL = CASE.get('profile', {}).get('label_start', 0x400000)
LEVELS = CASE.get('profile', {}).get('meta_dnode_levels', 7)


def candidates():
    roots, objsets, seen = {}, [], set()
    for done in sorted(glob.glob(f'{RAW}/chunk_*.tsv.gz.done')):
        with gzip.open(done[:-5], 'rt') as f:
            for line in f:
                c = line.rstrip('\n').split('\t')
                kv = dict(x.split('=', 1) for x in c[6:] if '=' in x)
                if c[1] == 'IND' and kv.get('child_level') == str(LEVELS - 2):
                    if c[5] in seen:
                        continue
                    seen.add(c[5])
                    roots[c[0]] = ('IND', c[0], c[2], c[3], c[4], int(kv['max_birth']))
                elif c[1] == 'OBJSET' and kv.get('nlevels') == str(LEVELS):
                    objsets.append(('OBJSET', c[0], c[2], c[3], c[4], int(kv['bp_birth']),
                                    f'{int(kv["bp_dva0"], 16) + LABEL:x}', c[5]))
    out = list(roots.values())
    for o in objsets:
        if o[6] not in roots and o[7] not in seen:
            seen.add(o[7])
            out.append(o[:6])
    return out


def run(limit):
    probed = {}
    if os.path.exists(PROBED):
        for line in open(PROBED):
            r = json.loads(line)
            probed[r['offset']] = r
    allc = candidates()
    by_off = {c[1]: c for c in allc}
    todo = [c for c in allc if c[1] not in probed]
    # While the scan is still reading the disk, probe only recent candidates: random reads of
    # old states would slow the sequential scan down. Older ones follow once the scan is done.
    scan_running = ssh('pgrep -x zscan').strip() != ''
    min_birth = int(os.environ.get('MIN_BIRTH_DURING_SCAN', 700000)) if scan_running else 0
    deferred = sum(1 for c in todo if c[5] < min_birth)
    todo = [c for c in todo if c[5] >= min_birth]
    todo.sort(key=lambda c: -c[5])
    batch = todo[:limit]
    if batch:
        inp = ''.join(f'{k} {o} {cp} {ps} {ls} {b}\n' for k, o, cp, ps, ls, b in batch)
        res = subprocess.run(['ssh', '-o', 'ServerAliveInterval=30', HOST, reader_cmd('batch')],
                             input=inp, capture_output=True, text=True, timeout=7200)
        with open(PROBED, 'a') as f:
            for line in res.stdout.splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                probed[r['offset']] = r
                f.write(line + '\n')

    def label(r):
        return classify(r.get('root')) if r.get('ok') else 'unreadable'

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
        table.append({'offset': r['offset'], 'kind': 'L6' if r['kind'] == 'IND' else r['kind'],
                      'birth': r['birth'], 'objects': r.get('objects'),
                      'root': ', '.join(r.get('root', [])[:12]) + (' …' if len(r.get('root', [])) > 12 else ''),
                      'status': 'verified ✓',
                      # how to read this candidate (for catalog rebuild / extraction)
                      'candidate': {'kind': r['kind'], 'offset': r['offset'], 'comp': c[2], 'psize': int(c[3]),
                                    'lsize': int(c[4])} if c else None})
        if len(table) >= 15:
            break
    os.makedirs(os.path.dirname(CANDIDATES), exist_ok=True)
    json.dump(table, open(CANDIDATES + '.tmp', 'w'), ensure_ascii=False)
    os.replace(CANDIDATES + '.tmp', CANDIDATES)
    pending = max(len(todo) - len(batch), 0)
    json.dump({'probed': len(probed), 'pending': pending, 'deferred': deferred, 'target': TARGET_NAME,
               'target_states': len(states), 'by_dataset': by_dataset}, open(PROBE_STATS, 'w'), ensure_ascii=False)
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
