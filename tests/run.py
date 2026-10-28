#!/usr/bin/env python3
"""Pipeline test runner (see tests/run.sh).

Per fixture variant: unpack the images (and destroy the objset block for <variant>-damaged), run
zscan, pick the tree-root candidates exactly like rank-trees.py does, verify them with
`zsread.py batch`, list the newest verified state of the target with `zsread.py tree`, extract
everything with `zsread.py extract` and compare every file (SHA-256, size, mtime, symlink target)
with the expectation recorded in the fixture's sidecar.

Result per variant: PASS | FAIL | XFAIL | XPASS | SKIP. Exit code = number of FAIL + XPASS lines.
"""
import gzip
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# rank-trees.py reads a case file when imported; give it an empty one (the runner passes the
# pool's parameters explicitly).
_EMPTY_CASE = os.path.join(tempfile.mkdtemp(prefix='zfs-salvage-case-'), 'empty.json')
with open(_EMPTY_CASE, 'w') as f:
    f.write('{}')
os.environ['ZFS_SALVAGE_CASE'] = _EMPTY_CASE

import fx  # noqa: E402

_spec = importlib.util.spec_from_file_location('rank_trees', os.path.join(fx.SCRIPTS, 'rank-trees.py'))
rank_trees = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rank_trees)

# Expected failures: variant -> ("planned" or "never", why). Applies to <variant> and
# <variant>-damaged. An entry that passes is reported as XPASS and counts as a failure, so the
# table has to be updated in the same commit that fixes the behaviour.
XFAIL = {
    'modern-offset': ('planned', 'pool does not start at offset 0 of the file'),
    'raidz1-4': ('planned', 'raidz is not supported'),
    'raidz2-5': ('planned', 'raidz is not supported'),
    'raidz1-4-missing': ('planned', 'raidz with a missing disk'),
    'unsupported-encrypted': ('never', 'native encryption is refused by make-case'),
    'unsupported-special-missing': ('never', 'metadata on the missing special vdev; refused'),
}


class Fail(Exception):
    pass


def log(verbose, msg):
    if verbose:
        print(f'    {msg}', file=sys.stderr, flush=True)


def vdev_map(side, paths):
    """Top-level vdev id -> device files, from the `zpool create` arguments in the sidecar
    (`mirror`/`raidz*` start a group, every other device is a top-level vdev of its own)."""
    toks, spec, i = side['create'], [], 0
    while i < len(toks):
        if toks[i] in ('-o', '-O'):
            i += 2
            continue
        if not toks[i].startswith('-'):
            spec.append(toks[i])
        i += 1
    groups, cur = [], None
    for t in spec:
        if t == 'mirror' or t.startswith('raidz'):
            cur = []
            groups.append(cur)
        elif t in ('special', 'log', 'dedup'):          # allocation class: the next device is a new vdev
            cur = None
        elif cur is not None:
            cur.append(paths[int(t)])
        else:
            groups.append([paths[int(t)]])
    return dict(enumerate(groups))


def zsread(zsdir, args, vdevs, label, stdin=b'', timeout=900):
    spec = ';'.join(f'{v}={",".join(p)}' for v, p in sorted(vdevs.items()) if p)
    env = dict(os.environ, ZSREAD_VDEVS=spec, ZSREAD_LABEL_START=str(label), LC_ALL='C.UTF-8')
    r = subprocess.run(['python3', '-I', os.path.join(zsdir, 'zsread.py'), *args], input=stdin, env=env,
                       capture_output=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr.decode('utf-8', 'replace')


def prepare_zsread(use_lib):
    """A private copy of zsread.py, with libzsalvage.so next to it (zsread looks there first)."""
    d = tempfile.mkdtemp(prefix='zfs-salvage-zsread-')
    shutil.copy(os.path.join(fx.SCRIPTS, 'zsread.py'), d)
    if use_lib:
        shutil.copy(fx.LIB, d)
    return d


def scan(dev, work, vdev=0, max_vdev=0):
    """Run zscan; write its finds as a scan chunk like scan-device.sh does. Returns the finds."""
    r = subprocess.run([fx.ZSCAN, '--max-vdev', str(max_vdev), dev], capture_output=True, text=True)
    if r.returncode:
        raise Fail(f'zscan exited {r.returncode}: {r.stderr.strip()[-200:]}')
    raw = os.path.join(work, f'raw{vdev}')
    os.makedirs(raw)
    with open(os.path.join(raw, 'vdev'), 'w') as f:
        f.write(str(vdev))
    with gzip.open(os.path.join(raw, 'chunk_0000.tsv.gz'), 'wt') as f:
        f.write(r.stdout)
    open(os.path.join(raw, 'chunk_0000.tsv.gz.done'), 'w').close()
    return raw, [l.split('\t') for l in r.stdout.splitlines()]


def describe_finds(finds):
    kinds = {}
    for c in finds:
        kinds[c[1]] = kinds.get(c[1], 0) + 1
    ind_levels = sorted({kv.split('=')[1] for c in finds if c[1] == 'IND' for kv in c[6:] if kv.startswith('child_level=')})
    nlevels = sorted({kv.split('=')[1] for c in finds if c[1] == 'OBJSET' for kv in c[6:] if kv.startswith('nlevels=')})
    return f'{len(finds)} finds {kinds}, IND child levels {ind_levels}, OBJSET nlevels {nlevels}'


def expected_files(side):
    gone = side.get('deleted_prefix')
    return [f for f in side.get('files', []) if not (gone and f['path'].startswith(gone))]


def pipeline(base, damaged, zsdir, verbose, derived=None):
    side = fx.sidecars()[base]
    work = fx.tmpdir()
    try:
        devs = fx.unpack(base, work)
        if damaged:
            fx.damage(work, base)
        vdevs = vdev_map(side, devs)
        if derived and 'embed_offset' in derived:                 # the pool starts inside a larger file
            big = os.path.join(work, 'embedded.img')
            with open(big, 'wb') as out, open(devs[0], 'rb') as src:
                out.seek(derived['embed_offset'])
                shutil.copyfileobj(src, out)
            vdevs = {v: [big if p == devs[0] else p for p in g] for v, g in vdevs.items()}
        if derived and 'omit' in derived:
            vdevs = {v: [p for p in g if os.path.basename(p) not in derived['omit']] for v, g in vdevs.items()}
        label = side['label_start']
        levels = (side.get('meta_dnode') or side.get('destroyed_meta') or {}).get('nlevels', 7)

        cands, finds, seen = [], [], set()
        for v, group in sorted(vdevs.items()):                   # one scan per top-level vdev
            if group:
                raw, f = scan(group[0], work, v, max(vdevs))
                finds += f
                cands += rank_trees.candidates(raw, levels, label, seen)
        log(verbose, f'{len(vdevs)} top-level vdev(s); ' + describe_finds(finds))
        if not cands:
            raise Fail(f'no tree-root candidate for {levels} levels: {describe_finds(finds)}')
        log(verbose, f'{len(cands)} candidates')

        inp = ''.join(f'{k} {o} {cp} {ps} {ls} {b}\n' for k, o, cp, ps, ls, b in cands).encode()
        rc, out, err = zsread(zsdir, ['batch'], vdevs, label, inp)
        results = [json.loads(l) for l in out.decode('utf-8', 'replace').splitlines() if l.startswith('{')]
        okay = [r for r in results if r.get('ok')]
        if not okay:
            errs = sorted({r.get('error', '?')[:80] for r in results})[:3]
            raise Fail(f'{len(cands)} candidates, none readable ({"; ".join(errs) or err.strip()[-120:]})')
        if 'zvol' in side:
            return zvol_pipeline(side, work, zsdir, vdevs, label, cands, okay, verbose)
        names = {f['path'].split('/')[1] for f in expected_files(side)}
        mine = [r for r in okay if names & set(r['root'])]
        if not mine:
            raise Fail(f'{len(okay)} readable trees, none has the expected top-level names '
                       f'{sorted(names)}; saw {sorted({n for r in okay for n in r["root"]})[:8]}')
        best = max(mine, key=lambda r: r['birth'])
        cand = next(c for c in cands if c[1] == best['offset'])
        carg = [cand[0], cand[1], cand[2], cand[3], cand[4]]
        state = f'state {best["kind"]} birth {best["birth"]}, {best.get("objects")} objects'
        log(verbose, f'{state} @{best["offset"]}')

        rc, out, err = zsread(zsdir, ['tree', *carg], vdevs, label)
        tree = {}
        for line in out.decode('utf-8', 'surrogateescape').splitlines():
            c = line.split('\t')
            if len(c) >= 6:
                tree[c[0]] = c
        want = expected_files(side)
        missing = [f['path'] for f in want if f['path'] not in tree or not tree[f['path']][5].startswith('ok')]
        if missing:
            raise Fail(f'tree lists {len(want) - len(missing)}/{len(want)} expected entries ({state}), '
                       f'missing e.g. {missing[0]}')
        wrong = [f['path'] for f in want if f['type'] == 'f' and str(f['size']) != tree[f['path']][2]]
        if wrong:
            raise Fail(f'{len(wrong)} entries with a wrong size in the tree, e.g. {wrong[0]}')

        lines = ''.join(f'{tree[f["path"]][4]}\t{f["path"]}\n' for f in want).encode('utf-8', 'surrogateescape')
        rc, tar_bytes, err = zsread(zsdir, ['extract', *carg], vdevs, label, lines)
        tar_path = os.path.join(work, 'out.tar')
        with open(tar_path, 'wb') as f:
            f.write(tar_bytes)
        members = {}
        with tarfile.open(tar_path) as tf:
            import hashlib
            for m in tf:
                if m.isreg():
                    h = hashlib.sha256()
                    src = tf.extractfile(m)
                    for b in iter(lambda: src.read(1 << 20), b''):
                        h.update(b)
                    members['/' + m.name] = ('f', m.size, h.hexdigest(), m.mtime)
                elif m.issym():
                    members['/' + m.name] = ('l', m.linkname)
        bad = []
        for f in want:
            got = members.get(f['path'])
            if got is None:
                bad.append((f['path'], 'not extracted'))
            elif f['type'] == 'l':
                if got != ('l', f['target']):
                    bad.append((f['path'], f'symlink -> {got[1:]}'))
            elif got[0] != 'f' or got[1] != f['size'] or got[2] != f['sha256']:
                bad.append((f['path'], 'content differs (sha256)'))
            elif got[3] != f['mtime']:
                bad.append((f['path'], f'mtime {got[3]} != {f["mtime"]}'))
        if bad:
            raise Fail(f'extract: {len(bad)}/{len(want)} differ, e.g. {bad[0][0]}: {bad[0][1]}')
        return f'files={len(want)}/{len(want)}'
    finally:
        fx.rmtree(work)


def zvol_pipeline(side, work, zsdir, vdevs, label, cands, okay, verbose):
    """A zvol fixture: the newest verified volume state must extract to the image the generator wrote."""
    want = side['zvol']
    mine = [r for r in okay if r.get('type') == 'zvol' and r.get('volsize') == want['size']]
    if not mine:
        raise Fail(f'{len(okay)} readable trees, none is a zvol of {want["size"]} bytes; '
                   f'zvols seen: {sorted({r.get("volsize") for r in okay if r.get("type") == "zvol"})}')
    best = max(mine, key=lambda r: r['birth'])
    cand = next(c for c in cands if c[1] == best['offset'])
    carg = list(cand[:5])
    log(verbose, f'zvol state {best["kind"]} birth {best["birth"]} @{best["offset"]}')
    rc, out, err = zsread(zsdir, ['tree', *carg], vdevs, label)
    lines = [l.split('\t') for l in out.decode().splitlines() if '\t' in l]
    if len(lines) != 1 or lines[0][0] != '/volume.raw' or lines[0][2] != str(want['size']):
        raise Fail(f'tree of a zvol should be one entry /volume.raw of {want["size"]} bytes, got {lines[:3]}')
    rc, tar_bytes, err = zsread(zsdir, ['extract', *carg], vdevs, label, f'{lines[0][4]}\t{lines[0][0]}\n'.encode())
    tar_path = os.path.join(work, 'zvol.tar')
    with open(tar_path, 'wb') as f:
        f.write(tar_bytes)
    manifest = [l.split('\t') for l in err.splitlines() if l.startswith('M\t')]
    import hashlib
    with tarfile.open(tar_path) as tf:
        members = tf.getmembers()
        if len(members) != 1:
            raise Fail(f'extract gave {len(members)} tar members')
        h = hashlib.sha256()
        src = tf.extractfile(members[0])
        for b in iter(lambda: src.read(1 << 20), b''):
            h.update(b)
        name, size = members[0].name, members[0].size
    if (name, size) != ('volume.raw', want['size']):
        raise Fail(f'tar member {name!r} of {size} bytes, expected volume.raw of {want["size"]}')
    if h.hexdigest() != want['sha256']:
        raise Fail(f'volume content differs (sha256 {h.hexdigest()[:12]} != {want["sha256"][:12]})')
    if not manifest or manifest[0][1] != 'OK' or manifest[0][4] != want['sha256']:
        raise Fail(f'manifest line does not report the same image: {manifest}')
    return f'volume={want["size"]} bytes, sha256 ok, holes {want.get("holes")}'


def plan(names):
    """(display name, base variant, damaged?, derived spec or None) for each requested variant."""
    real, der = fx.sidecars(), fx.derived()
    out = []
    if not names:
        for n, s in real.items():
            out.append((n, n, False, None))
            if (s.get('damage') or {}).get('spec'):
                out.append((n + '-damaged', n, True, None))
        for n, d in der.items():
            out.append((n, d['base'], False, d))
        return out
    for n in names:
        if n in real:
            out.append((n, n, False, None))
        elif n.endswith('-damaged') and n[:-8] in real:
            out.append((n, n[:-8], True, None))
        elif n in der:
            out.append((n, der[n]['base'], False, der[n]))
        else:
            out.append((n, None, False, None))
    return out


def write_matrix(results, path):
    """tests/capabilities.json: what each capability of tests/capability_map.json did in this run. Deterministic (no timings),
    so that a changed file in git means a changed capability."""
    with open(os.path.join(HERE, 'capability_map.json')) as f:
        cmap = json.load(f)
    caps = {}
    for display, status in results:
        base = display[:-8] if display.endswith('-damaged') else display
        for area, cap in cmap['variants'].get(base, []):
            e = caps.setdefault((area, cap), {'area': area, 'capability': cap, 'variants': [], 'statuses': set()})
            e['variants'].append(display)
            e['statuses'].add(status)
    out = []
    for e in caps.values():
        st = e.pop('statuses')
        e['status'] = 'supported' if st == {'PASS'} else 'not yet' if st == {'XFAIL'} else 'mixed: ' + '/'.join(sorted(st))
        e['variants'] = sorted(e['variants'])
        out.append(e)
    out.sort(key=lambda e: (cmap['areas'].index(e['area']), e['capability']))
    with open(path, 'w') as f:
        json.dump({'capabilities': out}, f, indent=1)
        f.write('\n')
    return [e for e in out if e['status'] not in ('supported', 'not yet')]


def main():
    args = sys.argv[1:]
    verbose = '-v' in args
    use_lib = '--no-lib' not in args
    matrix = '--matrix' in args
    names = [a for a in args if not a.startswith('-')]
    results = []
    if not fx.ensure_built():
        sys.exit('cannot build zscan / libzsalvage.so (gcc missing?)')
    zsdir = prepare_zsread(use_lib)
    failures = 0
    try:
        for display, base, damaged, derived in plan(names):
            if base is None:
                print(f'FAIL {display} unknown variant')
                failures += 1
                continue
            if not fx.have_images(base):
                print(f'{"FAIL" if names else "SKIP"} {display} no images in tests/fixtures/{base}/ '
                      f'(tests/vm/run.sh tests/make-fixtures.sh)')
                failures += bool(names)
                continue
            t0 = time.time()
            try:
                detail, ok = pipeline(base, damaged, zsdir, verbose, derived), True
            except Fail as e:
                detail, ok = str(e), False
            except Exception as e:  # noqa: BLE001 - a crashing pipeline is a result, not a crash of the runner
                detail, ok = f'{type(e).__name__}: {e}', False
            expect = XFAIL.get(display) or XFAIL.get(base)
            if expect and ok:
                status, detail = 'XPASS', f'{detail} (listed as an expected failure: remove it from XFAIL)'
                failures += 1
            elif expect:
                until = '' if expect[0] == 'never' else '; planned'
                status, detail = 'XFAIL', f'{detail}  [XFAIL: {expect[1]}{until}]'
            elif ok:
                status = 'PASS'
            else:
                status = 'FAIL'
                failures += 1
            results.append((display, status))
            print(f'{status} {display} {detail}' + (f'  ({time.time() - t0:.1f} s)' if verbose else ''), flush=True)
    finally:
        shutil.rmtree(zsdir, ignore_errors=True)
    if matrix:
        if names or failures or any(st not in ('PASS', 'XFAIL') for _, st in results) or not results:
            print('no matrix written: it needs a complete run of every variant without FAIL, XPASS or SKIP', file=sys.stderr)
            failures += 1
        else:
            bad = write_matrix(results, os.path.join(HERE, 'capabilities.json'))
            for e in bad:
                print(f'{e["status"]} {e["area"]}: {e["capability"]} ({", ".join(e["variants"])})', file=sys.stderr)
            failures += len(bad)
            print('wrote tests/capabilities.json')
    sys.exit(min(failures, 255))


if __name__ == '__main__':
    main()
