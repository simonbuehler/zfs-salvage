#!/usr/bin/env python3
"""Check that a case file has the fields and types the tools rely on.

Usage: validate_case.py [--partial] <case.json> ...
  --partial   accept a case that make-case could only fill from labels (pool did not open: no timeline,
              no meta-dnode profile); everything else stays required
Exit code 0 = valid, 1 = problems (printed one per line).
"""
import json
import sys


def validate(case, partial=False):
    bad = []

    def need(obj, path, types, nonempty=False):
        cur = obj
        for k in path.split('.'):
            if not isinstance(cur, dict) or k not in cur:
                bad.append(f'missing: {path}')
                return None
            cur = cur[k]
        if not isinstance(cur, types) or isinstance(cur, bool) and bool not in (types if isinstance(types, tuple) else (types,)):
            bad.append(f'{path}: expected {types}, got {type(cur).__name__}')
            return None
        if nonempty and not cur:
            bad.append(f'{path}: empty')
        return cur

    need(case, 'name', str, True)
    need(case, 'pool.name', str, True)
    need(case, 'pool.topology', str)
    need(case, 'pool.ashift', int)
    devs = need(case, 'pool.devices', list, True)
    for i, d in enumerate(devs or []):
        if not isinstance(d, dict) or not isinstance(d.get('path'), str):
            bad.append(f'pool.devices[{i}].path: expected a string')
        elif 'vdev' in d and not isinstance(d['vdev'], (int, type(None))):
            bad.append(f'pool.devices[{i}].vdev: expected an integer')
    need(case, 'profile.label_start', int)
    need(case, 'profile.compression', list)
    need(case, 'profile.checksum', list)
    if not partial:
        need(case, 'profile.meta_dnode_levels', int)
        need(case, 'profile.indirect_block_size', int)
        tl = need(case, 'timeline', list, True)
        for i, e in enumerate((tl or [])[:3]):
            if not (isinstance(e, dict) and isinstance(e.get('txg'), int) and isinstance(e.get('time'), int)):
                bad.append(f'timeline[{i}]: expected {{"txg": int, "time": int}}')
    need(case, 'target.dataset', str, True)
    need(case, 'target.root_names', list)
    need(case, 'compatibility', list)
    need(case, 'scan.device', str)
    need(case, 'scan.chunk_dir', str)
    need(case, 'scan.log', str)
    if case.get('unsupported'):
        bad.append('the case lists unsupported properties (written with --force): ' + '; '.join(case['unsupported']))
    return bad


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if a != '--partial']
    if not args:
        sys.exit(__doc__)
    rc = 0
    for path in args:
        with open(path) as f:
            problems = validate(json.load(f), '--partial' in sys.argv)
        for p in problems:
            print(f'{path}: {p}')
        rc |= bool(problems)
    sys.exit(rc)
