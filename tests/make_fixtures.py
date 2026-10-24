#!/usr/bin/env python3
"""Create the test fixtures: small real ZFS pools in image files, plus a JSON sidecar each.

Runs as root on a Linux machine with the ZFS kernel module (normally inside the test VM:
tests/vm/run.sh tests/make-fixtures.sh). Never run it on a machine with real pools you care about
– it only touches files below WORK, but it does create and export pools.

Output (tests/fixtures/):
  <variant>/<dev>.img.xz      the vdev image files of a variant, xz-compressed
  <variant>.json              sidecar: pool facts from zdb, the expected files, damage spec
Derived variants (no own images): their sidecar names a "base" variant and a transformation
(apply the damage spec, leave out devices, embed at an offset). tests/run.sh applies it.
"""
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import zlib

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(REPO, 'tests', 'fixtures')
WORK = '/var/tmp/zfs-salvage-fixtures'
LABEL_START = 4 << 20
DEV_SIZE = 128 << 20
BASE_TIME = 1400000000          # 2014-05-13, start of the deterministic mtimes


def sh(*cmd, check=True, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if check and r.returncode:
        raise RuntimeError(f'{" ".join(cmd)}: {r.stderr.strip()[-500:]}')
    return r.stdout


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


# ---------------------------------------------------------------- file content
def populate(root, seed, flavour='home'):
    """Write a deterministic file set below root; returns the expected file list."""
    rnd = random.Random(seed)
    files = []
    words = ['alpha', 'beta', 'gamma', 'delta', 'photo', 'trip', 'summer', 'winter', 'zfs', 'disk']

    def put(rel, data=None, link=None, mtime=None):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        t = mtime if mtime is not None else BASE_TIME + len(files) * 3607
        if link is not None:
            os.symlink(link, p)
            files.append({'path': '/' + rel, 'type': 'l', 'target': link})
            return
        with open(p, 'wb') as f:
            f.write(data)
        os.utime(p, (t, t))
        files.append({'path': '/' + rel, 'type': 'f', 'size': len(data),
                      'sha256': hashlib.sha256(data).hexdigest(), 'mtime': t})

    if flavour == 'var':
        for i in range(30):
            put(f'log/app{i % 3}/log-{i:03d}.txt',
                ''.join(f'{i} {rnd.choice(words)} event {j}\n' for j in range(rnd.randint(50, 400))).encode())
        put('cache/state.bin', rnd.randbytes(64 << 10))
        return files

    dirs = ['docs', 'docs/2014', 'photos/2013-12', 'photos/2014-04', 'a/b/c/d/e']
    for i in range(40):                                    # small text files, nested
        text = ''.join(f'{rnd.choice(words)} {rnd.choice(words)} {j}\n' for j in range(rnd.randint(30, 900)))
        put(f'{dirs[i % len(dirs)]}/note-{i:02d}.txt', text.encode())
    for i in range(6):                                     # "photos": header + compressible + random part
        body = (b'\xff\xd8\xff\xe0' + bytes(range(256)) * rnd.randint(2000, 6000)
                + rnd.randbytes(48 << 10))
        put(f'photos/{"2013-12" if i < 3 else "2014-04"}/img_{i:04d}.jpg', body)
    put('photos/Ausflüge Fertig/Bild ä 01.jpg', b'\xff\xd8\xff\xe0' + bytes(range(256)) * 900)  # UTF-8 name
    for i in range(2):                                     # incompressible data blocks
        put(f'bin/random-{i}.bin', rnd.randbytes(256 << 10))
    big = b''.join(f'{k:012d} large compressible file with several levels of indirection\n'.encode()
                   for k in range(40 << 20 >> 6))          # ~40 MiB: needs L1/L2 indirect blocks
    put('video/big.mts', big)
    put('docs/empty.txt', b'')
    put('docs/tiny.txt', b'tiny file stored as embedded data in the block pointer\n')
    put('.cache/thumb.png', b'\x89PNG' + bytes(range(256)) * 8)
    put('photos/latest', link='2014-04')
    return files


# ---------------------------------------------------------------- zdb facts
DVA_RE = re.compile(r'DVA\[\d\]=<(\d+):([0-9a-f]+):([0-9a-f]+)>')


def datasets(devdir, pool):
    return parse_dataset_lines(sh('zdb', '-e', '-p', devdir, '-d', pool, check=False))


def parse_dataset_lines(out):
    res = {}
    for line in out.splitlines():
        m = re.match(r'^Dataset (\S+) \[(\w+)\], ID (\d+), cr_txg (\d+), (\S+), (\d+) objects(?:, rootbp (.*))?$', line)
        if not m:
            continue
        name, typ, did, cr, size, objs, bp = m.groups()
        d = {'type': typ, 'id': int(did), 'objects': int(objs), 'size': size}
        if bp:
            d['rootbp'] = bp
            d['dvas'] = [{'vdev': int(v), 'offset': int(o, 16), 'asize': int(a, 16)} for v, o, a in DVA_RE.findall(bp)]
            sz = re.search(r'size=([0-9a-f]+)L/([0-9a-f]+)P', bp)
            d['lsize'], d['psize'] = (int(sz.group(1), 16), int(sz.group(2), 16)) if sz else (None, None)
            d['birth'] = int(re.search(r'birth=(\d+)L', bp).group(1)) if 'birth=' in bp else None
            d['objset_block'] = ' '.join(bp.split('] ', 1)[1].split()[:3]) if '] ' in bp else ''
        res[name] = d
    return res, out


def meta_dnode(devdir, pool, ds):
    out = sh('zdb', '-e', '-p', devdir, '-dddd', f'{pool}/{ds}', '0', check=False)
    m = re.search(r'^\s+0\s+(\d+)\s+(\S+)\s+(\S+)\s', out, re.M)
    unit = {'K': 1 << 10, 'M': 1 << 20}

    def size(x):
        return int(float(x[:-1]) * unit[x[-1]]) if x[-1] in unit else int(x)
    first = out.splitlines()[0] if out else ''
    ind = sh('zdb', '-e', '-p', devdir, '-ddddd', f'{pool}/{ds}', '0', check=False)
    blocks = [l.strip() for l in ind.splitlines() if re.match(r'^\s+[0-9a-f]+\s+L\d+\s', l)][:12]
    return ({'nlevels': int(m.group(1)), 'indirect_block_size': size(m.group(2)),
             'data_block_size': size(m.group(3))} if m else {}), blocks, first


def newest_txg(dev):
    out = sh('zdb', '-lu', dev, check=False)
    return max((int(x) for x in re.findall(r'^\s+txg = (\d+)', out, re.M)), default=None)


def current_txg(pool):
    out = sh('zdb', '-u', pool, check=False)
    m = re.search(r'txg = (\d+)', out)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------- variants
# layout: list of (role, count); role 'data' = top-level vdev spec built from create spec
VARIANTS = {
    'legacy-single': {'devs': 1, 'spec': '{0}', 'create': ['-d', '-o', 'ashift=9']},
    'modern-single': {'devs': 1, 'spec': '{0}', 'create': []},
    'modern-mirror': {'devs': 2, 'spec': 'mirror {0} {1}', 'create': []},
    'modern-stripe': {'devs': 2, 'spec': '{0} {1}', 'create': []},
    'modern-sha256': {'devs': 1, 'spec': '{0}', 'create': ['-O', 'checksum=sha256']},
    'modern-zvol': {'devs': 1, 'spec': '{0}', 'create': [], 'zvol': True},
    'modern-destroyed': {'devs': 1, 'spec': '{0}', 'create': [], 'destroy': True},
    'modern-deleted': {'devs': 1, 'spec': '{0}', 'create': [], 'delete': 'docs'},
    'modern-twodatasets': {'devs': 1, 'spec': '{0}', 'create': [], 'second': 'var'},
    'raidz1-4': {'devs': 4, 'spec': 'raidz1 {0} {1} {2} {3}', 'create': []},
    'raidz2-5': {'devs': 5, 'spec': 'raidz2 {0} {1} {2} {3} {4}', 'create': []},
    'unsupported-encrypted': {'devs': 1, 'spec': '{0}', 'create': [], 'encrypt': True},
    'unsupported-largednode': {'devs': 1, 'spec': '{0}', 'create': ['-O', 'dnodesize=auto', '-O', 'xattr=sa'],
                               'xattrs': True},
    'unsupported-special': {'devs': 2, 'spec': '{0} special {1}', 'create': []},
}
DERIVED = {
    'modern-offset': {'base': 'modern-single', 'embed_offset': (1 << 20) + 7 * 512,
                      'purpose': 'vdev start finder'},
    'raidz1-4-missing': {'base': 'raidz1-4', 'omit': ['d3.img'], 'purpose': 'refused until raidz parity reconstruction exists'},
    'unsupported-special-missing': {'base': 'unsupported-special', 'omit': ['d1.img'],
                                    'purpose': 'special vdev (metadata) missing: clear refusal'},
}
SKIPPED = {'modern-hightxg': 'needs ~1,000,000 TXGs; not generated (the --max-txg check is tested instead)'}


def damage_spec(target, devs, kind, dvas):
    """Byte ranges that destroy every copy of the target's objset block (mirror/single/stripe only)."""
    if kind.startswith('raidz'):
        return None, 'raidz: physical location of a DVA needs the raidz map; damaged variant later'
    spec = []
    for i, d in enumerate(dvas):
        targets = devs if kind == 'mirror' else [devs[d['vdev']]]
        for dev in targets:
            spec.append({'dev': os.path.basename(dev), 'offset': LABEL_START + d['offset'],
                         'length': d['asize'], 'seed': 1000 + i})
    return spec, None


def apply_damage(devdir, spec):
    for s in spec:
        rnd = random.Random(s['seed'])
        with open(os.path.join(devdir, s['dev']), 'r+b') as f:
            f.seek(s['offset'])
            f.write(rnd.randbytes(s['length']))


def make(variant, cfg):
    t0 = time.time()
    pool = f'fx-{variant}'
    vdir = os.path.join(WORK, variant)
    shutil.rmtree(vdir, ignore_errors=True)
    os.makedirs(vdir)
    devs = [os.path.join(vdir, f'd{i}.img') for i in range(cfg['devs'])]
    for d in devs:
        with open(d, 'wb') as f:
            f.truncate(DEV_SIZE)
    create = cfg['create'] if '-o' in cfg['create'] and 'ashift=9' in cfg['create'] else ['-o', 'ashift=12'] + cfg['create']
    mnt = f'/mnt/{pool}'
    sh('zpool', 'create', '-f', *create, '-O', f'mountpoint={mnt}', '-O', 'atime=off', pool,
       *cfg['spec'].format(*devs).split())
    side = {'variant': variant, 'pool': pool, 'zfs_version': sh('zfs', 'version').split()[0],
            'create': create + cfg['spec'].replace('{', '').replace('}', '').split(),
            'devices': [os.path.basename(d) for d in devs], 'device_size': DEV_SIZE,
            'label_start': LABEL_START}
    target = 'home'
    if cfg.get('encrypt'):
        key = os.path.join(vdir, 'key')
        with open(key, 'wb') as f:
            f.write(bytes(range(32)))
        sh('zfs', 'create', '-o', 'encryption=on', '-o', 'keyformat=raw', '-o', f'keylocation=file://{key}',
           f'{pool}/home')
    elif cfg.get('zvol'):
        target = 'vol'
        sh('zfs', 'create', '-s', '-V', '64M', f'{pool}/vol')    # sparse: no reservation in the small pool
        sh('udevadm', 'settle')
        rnd = random.Random(7)
        data = b''.join((bytes([k % 251]) * 4096 if k % 8 else rnd.randbytes(4096))
                        for k in range(64 << 20 >> 12))
        with open(f'/dev/zvol/{pool}/vol', 'wb') as f:
            f.write(data)
        side['zvol'] = {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    if target == 'home':
        if not cfg.get('encrypt'):
            sh('zfs', 'create', f'{pool}/home')
        side['files'] = populate(f'{mnt}/home', seed=zlib.crc32(variant.encode()))
        if cfg.get('xattrs'):
            for f in side['files'][:20]:
                if f['type'] == 'f':
                    os.setxattr(f'{mnt}/home{f["path"]}', 'user.comment', b'x' * 400)
        if cfg.get('second'):
            sh('zfs', 'create', f'{pool}/{cfg["second"]}')
            side['files_second'] = populate(f'{mnt}/{cfg["second"]}', seed=99, flavour='var')
            side['second_dataset'] = cfg['second']
    sh('zpool', 'sync', pool)
    side['features'] = dict(l.split('\t') for l in sh('zpool', 'get', '-H', '-o', 'property,value', 'all', pool).splitlines()
                            if l.startswith('feature@'))
    if cfg.get('delete'):
        side['txg_before_delete'] = current_txg(pool)
        shutil.rmtree(f'{mnt}/home/{cfg["delete"]}')
        sh('zpool', 'sync', pool)
        side['deleted_prefix'] = '/' + cfg['delete'] + '/'
    if cfg.get('destroy'):
        side['txg_before_destroy'] = current_txg(pool)
        side['destroyed_meta'] = meta_dnode_from_pool(pool)
        sh('zfs', 'destroy', '-r', f'{pool}/home')
        sh('zpool', 'sync', pool)
    sh('zpool', 'export', pool)

    ds, raw = datasets(vdir, pool)
    side['datasets'] = ds
    side['newest_txg'] = newest_txg(devs[0])
    tgt = f'{pool}/{target}'
    side['target'] = tgt
    if tgt in ds:
        side['meta_dnode'], side['meta_dnode_blocks'], first = meta_dnode(vdir, pool, target)
        bp_ds, _ = parse_dataset_lines(first)          # the -dddd header line carries the rootbp
        if tgt in bp_ds:
            ds[tgt].update(bp_ds[tgt])
    kind = cfg['spec'].split()[0] if cfg['spec'].split()[0] in ('mirror', 'raidz1', 'raidz2') else 'plain'
    side['layout'] = kind
    if tgt in ds and 'dvas' in ds[tgt] and not variant.startswith('unsupported'):
        spec, why = damage_spec(tgt, devs, kind, ds[tgt]['dvas'])
        if spec:
            tmp = vdir + '-damaged'
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.copytree(vdir, tmp)
            apply_damage(tmp, spec)
            out = ''
            for args in (['-d', pool], ['-d', tgt]):             # zdb reports problems on stderr
                r = subprocess.run(['zdb', '-e', '-p', tmp, *args], stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
                out += r.stdout + (f'\n[exit {r.returncode}]' if r.returncode else '')
            errs = [l.strip() for l in out.splitlines()
                    if re.search(r"error|can't|failed|unable|\[exit", l, re.I)]
            side['damage'] = {'spec': spec, 'zdb_error': ' | '.join(dict.fromkeys(errs))[:500]}
            shutil.rmtree(tmp)
        else:
            side['damage'] = {'spec': None, 'note': why}
    # package
    os.makedirs(os.path.join(OUT, variant), exist_ok=True)
    for d in devs:
        sh('xz', '-T0', '-9', '-k', '-f', d)
        shutil.move(d + '.xz', os.path.join(OUT, variant, os.path.basename(d) + '.xz'))
    side['image_sha256'] = {os.path.basename(d): sha256_file(d) for d in devs}
    side['seconds'] = round(time.time() - t0, 1)
    json.dump(side, open(os.path.join(OUT, f'{variant}.json'), 'w'), indent=1, ensure_ascii=False)
    shutil.rmtree(vdir)
    print(f'{variant}: {len(side.get("files", []))} files, txg {side["newest_txg"]}, '
          f'meta {side.get("meta_dnode")}, objset block {ds.get(tgt, {}).get("objset_block")}, '
          f'damage {"yes" if (side.get("damage") or {}).get("spec") else "no"}, {side["seconds"]} s', flush=True)


def meta_dnode_from_pool(pool):
    out = sh('zdb', '-dddd', f'{pool}/home', '0', check=False)
    m = re.search(r'^\s+0\s+(\d+)\s+(\S+)\s+(\S+)\s', out, re.M)
    return {'nlevels': int(m.group(1)), 'iblk': m.group(2), 'dblk': m.group(3)} if m else {}


def main():
    if os.geteuid() != 0:
        sys.exit('run as root (inside the test VM: tests/vm/run.sh tests/make-fixtures.sh)')
    only = sys.argv[1:]
    os.makedirs(WORK, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)
    for name, cfg in VARIANTS.items():
        if not only or name in only:
            try:
                make(name, cfg)
            except Exception as e:  # noqa: BLE001 - report and continue with the next variant
                print(f'{name}: FAILED {type(e).__name__}: {e}', flush=True)
                subprocess.run(['zpool', 'export', '-f', f'fx-{name}'], capture_output=True)
    for name, d in DERIVED.items():
        if not only or name in only or d['base'] in only:
            json.dump({'variant': name, **d}, open(os.path.join(OUT, f'{name}.json'), 'w'), indent=1)
    json.dump(SKIPPED, open(os.path.join(OUT, 'skipped.json'), 'w'), indent=1)
    print('done')


if __name__ == '__main__':
    main()
