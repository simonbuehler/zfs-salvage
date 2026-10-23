#!/usr/bin/env python3
"""make-case - inspect a damaged ZFS pool (read-only) and write a case file for zfs-salvage.

Runs on the recovery host. Uses `zdb` (a recent OpenZFS build is recommended) and only
reads from the devices. Everything that is specific to one pool ends up in the case file;
the other tools read it instead of hardcoding assumptions.

Steps: find devices -> read labels -> uberblock timeline -> try to open the pool at
recent TXGs -> list datasets (readable / unreadable) -> derive the on-disk profile ->
ask for the target dataset -> compatibility check -> write cases/<name>.json

Usage:
  make-case.py [--zdb zdb] [--dev /dev/X ...] [--target pool/ds] [--root-names a,b]
               [--name NAME] [--out DIR] [--yes]
Without --yes it asks interactively (defaults in brackets).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time

# What zfs-salvage currently supports. Extend here when you add support.
SUPPORTED = {
    'topology': {'disk', 'mirror'},
    'compression': {'lzjb', 'off', 'uncompressed', 'on'},
    'checksum': {'fletcher4'},
    'max_top_level_vdevs': 1,
}
HINTS = {
    'lz4': 'Add an LZ4 decompressor to tools/libzsalvage (zs_read_verify) and to zscan try_offset().',
    'zstd': 'Add zstd (ZFS uses a 4-byte header before the frame) to libzsalvage and zscan.',
    'gzip': 'Add zlib inflate (gzip-1..9) to libzsalvage and zscan.',
    'raidz': 'RAIDZ: scan each child disk and map physical to logical offsets using the raidz geometry; '
             'reads must reconstruct from data columns (and parity on errors).',
    'draid': 'dRAID is not supported; see the raidz hint plus the permutation tables.',
    'stripe': 'Several top-level vdevs: zscan must accept vdev ids > 0 and map each DVA to its device.',
    'sha256': 'Add SHA-256 verification to libzsalvage (checksum id 8).',
    'large_dnode': 'large_dnode: dnodes may span several 512-byte slots (dn_extra_slots); adapt zscan/zsread.',
    'encryption': 'Native encryption: data and most metadata are encrypted; requires the key and AES-GCM/CCM.',
    'embedded_data': 'embedded_data: small blocks live inside the block pointer; implement BP_IS_EMBEDDED decoding.',
}


def sh(cmd, timeout=600):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.stdout + r.stderr


def ask(prompt, default, assume_yes):
    if assume_yes:
        return default
    v = input(f'{prompt} [{default}]: ').strip()
    return v or default


# ------------------------------------------------------------------ discovery
def find_devices():
    out = sh(['lsblk', '-pnro', 'NAME,TYPE,FSTYPE,SIZE,RO'])
    devs = []
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 3 and f[2] == 'zfs_member':
            devs.append(f[0])
    return devs


def read_label(zdb, dev):
    out = sh([zdb, '-l', dev])
    lab = {'device': dev}
    m = re.search(r"^\s+name: '([^']+)'", out, re.M)
    lab['pool'] = m.group(1) if m else None
    for key in ('pool_guid', 'txg', 'guid', 'top_guid', 'ashift', 'version', 'vdev_children'):
        m = re.search(rf'^\s+{key}: (\d+)', out, re.M)
        lab[key] = int(m.group(1)) if m else None
    types = re.findall(r"^\s+type: '([^']+)'", out, re.M)
    lab['vdev_types'] = types
    lab['children_paths'] = re.findall(r"^\s+path: '([^']+)'", out, re.M)
    feats = re.search(r'features_for_read:\n((?:\s+\S+\n)*)', out)
    lab['features_for_read'] = [x.strip() for x in feats.group(1).splitlines()] if feats else []
    if 'failed to unpack label' in out and not lab['pool']:
        lab['error'] = 'no readable label'
    return lab


def read_uberblocks(zdb, devs):
    ubs = {}
    for d in devs:
        out = sh([zdb, '-lu', d])
        txg = None
        for line in out.splitlines():
            m = re.search(r'\btxg = (\d+)', line)
            if m:
                txg = int(m.group(1))
            m = re.search(r'timestamp = (\d+)', line)
            if m and txg is not None:
                ubs.setdefault(txg, int(m.group(1)))
    return sorted(ubs.items())


def try_open(zdb, devdir, pool, txgs, limit=40):
    """Newest TXGs first; returns (txg, zdb -d output) of the first TXG where the pool opens."""
    tried = []
    for txg in sorted(txgs, reverse=True)[:limit]:
        out = sh([zdb, '-e', '-p', devdir, '-t', str(txg), '-d', pool], timeout=900)
        ok = 'Dataset ' in out and "can't open" not in out.split('Dataset ')[0]
        tried.append((txg, ok))
        if ok:
            return txg, out, tried
    return None, '', tried


def parse_datasets(out):
    ds = []
    for m in re.finditer(r'^Dataset (\S+) \[(\w+)\], ID (\d+), cr_txg (\d+), (\S+), (\d+) objects, rootbp (.*)$',
                         out, re.M):
        name, typ, did, cr, size, objs, bp = m.groups()
        comp = re.search(r'\] (\w+) (\w+) (unencrypted|encrypted)', bp)
        birth = re.search(r'birth=(\d+)L', bp)
        ds.append({'name': name, 'type': typ, 'id': int(did), 'cr_txg': int(cr), 'size': size,
                   'objects': int(objs), 'checksum': comp.group(1) if comp else None,
                   'compression': comp.group(2) if comp else None,
                   'encrypted': bool(comp and comp.group(3) == 'encrypted'),
                   'objset_birth': int(birth.group(1)) if birth else None, 'readable': True})
    for m in re.finditer(r"failed to hold dataset '([^']+)': (.*)$", out, re.M):
        ds.append({'name': m.group(1), 'readable': False, 'error': m.group(2).strip()})
    return ds


def meta_dnode_profile(zdb, devdir, pool, txg, ds_name):
    """Indirect block size and levels of the meta-dnode of a readable dataset (object 0)."""
    out = sh([zdb, '-e', '-p', devdir, '-t', str(txg), '-dddd', ds_name, '0'], timeout=300)
    m = re.search(r'^\s+0\s+(\d+)\s+(\S+)\s+(\S+)\s+\S+\s+(\S+)?.*DMU dnode', out, re.M)
    if not m:
        return {}
    to_bytes = lambda s: int(float(s[:-1]) * {'K': 1024, 'M': 1 << 20}[s[-1]]) if s[-1] in 'KM' else int(s)
    return {'meta_dnode_levels': int(m.group(1)), 'indirect_block_size': to_bytes(m.group(2)),
            'dnode_block_size': to_bytes(m.group(3))}


def target_info(zdb, devdir, pool, txg, ds_name):
    """Head dataset object of an unreadable dataset via the MOS (birth of its objset bp)."""
    out = sh([zdb, '-e', '-p', devdir, '-t', str(txg), '-dddd', f'{pool}/0'], timeout=900)
    info = {}
    # find the DSL directory whose name maps to ds_name is not trivial from zdb output; use used_bytes
    for block in out.split('\n\n'):
        if 'DSL dataset' in block and 'bp = ' in block:
            m = re.search(r'\bbp = .*birth=(\d+)L.*fill=(\d+)', block)
            if m:
                info.setdefault('objset_candidates', []).append(
                    {'birth': int(m.group(1)), 'fill': int(m.group(2))})
    return info


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--zdb', default='zdb')
    ap.add_argument('--dev', action='append', default=[])
    ap.add_argument('--target')
    ap.add_argument('--root-names', default='')
    ap.add_argument('--name')
    ap.add_argument('--out', default='cases')
    ap.add_argument('--yes', action='store_true', help='accept defaults, no questions')
    a = ap.parse_args()

    print('== 1. devices')
    devs = a.dev or find_devices()
    if not devs:
        sys.exit('No ZFS member devices found. Pass --dev (e.g. an opened LUKS mapper). '
                 'Open encrypted devices READ-ONLY: cryptsetup open --readonly ...')
    for d in devs:
        ro = sh(['blockdev', '--getro', d]).strip()
        print(f'   {d}  read-only={ro}' + ('' if ro == '1' else '   <-- WARNING: not read-only (blockdev --setro / overlay recommended)'))

    print('== 2. labels')
    labels = [read_label(a.zdb, d) for d in devs]
    pool = next((l['pool'] for l in labels if l.get('pool')), None)
    if not pool:
        sys.exit('No readable ZFS label on the given devices.')
    lab = next(l for l in labels if l.get('pool'))
    topo = next((t for t in lab['vdev_types'] if t in ('mirror', 'raidz', 'draid')), 'disk')
    print(f"   pool {pool}, guid {lab['pool_guid']}, topology {topo}, ashift {lab['ashift']}, "
          f"top-level vdevs {lab['vdev_children']}, label txg {lab['txg']}")

    print('== 3. uberblock timeline')
    ubs = read_uberblocks(a.zdb, devs)
    if ubs:
        fmt = lambda t: time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(t))
        print(f'   {len(ubs)} uberblocks, txg {ubs[0][0]} ({fmt(ubs[0][1])} UTC) .. {ubs[-1][0]} ({fmt(ubs[-1][1])} UTC)')

    print('== 4. opening the pool read-only at recent TXGs (zdb, no import)')
    devdir = tempfile.mkdtemp(prefix='zfs-salvage-devs-')
    for d in devs:
        os.symlink(d, os.path.join(devdir, os.path.basename(d)))
    txg, out, tried = try_open(a.zdb, devdir, pool, [t for t, _ in ubs])
    datasets = parse_datasets(out) if txg else []
    print(f'   opens at txg {txg}' if txg else f'   pool does not open at the {len(tried)} newest TXGs '
          '(scanning the disk is still possible; profile will use defaults)')
    unreadable = [d for d in datasets if not d['readable']]
    readable = [d for d in datasets if d['readable'] and d.get('type') == 'ZPL' and '@' not in d['name']]
    for d in unreadable:
        print(f"   UNREADABLE {d['name']}: {d['error']}")

    print('== 5. on-disk profile')
    comps = sorted({d['compression'] for d in datasets if d.get('compression')}) or ['unknown']
    sums = sorted({d['checksum'] for d in datasets if d.get('checksum')}) or ['unknown']
    prof = {'compression': comps, 'checksum': sums, 'label_start': 4 << 20,
            'ashift': lab['ashift'], 'encrypted': any(d.get('encrypted') for d in datasets)}
    if readable and txg:
        prof.update(meta_dnode_profile(a.zdb, devdir, pool, txg, readable[0]['name']))
    print(f'   {prof}')

    print('== 6. target dataset')
    default_target = a.target or (unreadable[0]['name'] if unreadable else (readable[0]['name'] if readable else pool))
    target = ask('Dataset to recover', default_target, a.yes)
    names = ask('Names you expect in its root directory (comma separated, helps recognising it)',
                a.root_names, a.yes)
    name = a.name or ask('Case name', f'{pool}-{time.strftime("%Y%m%d")}', a.yes)

    print('== 7. compatibility check')
    issues = []
    if topo not in SUPPORTED['topology']:
        issues.append(('topology', topo, HINTS.get(topo, '')))
    if (lab.get('vdev_children') or 1) > SUPPORTED['max_top_level_vdevs']:
        issues.append(('top-level vdevs', lab['vdev_children'], HINTS['stripe']))
    for c in comps:
        if c not in SUPPORTED['compression'] and c != 'unknown':
            issues.append(('compression', c, HINTS.get(c.split('-')[0], f'add a {c} decompressor')))
    for c in sums:
        if c not in SUPPORTED['checksum'] and c != 'unknown':
            issues.append(('checksum', c, HINTS.get(c, f'add {c} verification')))
    for f in lab.get('features_for_read', []):
        key = f.split(':')[-1]
        if key in HINTS:
            issues.append(('feature', f, HINTS[key]))
    if prof.get('encrypted'):
        issues.append(('feature', 'encryption', HINTS['encryption']))
    if prof.get('indirect_block_size') not in (None, 16384):
        issues.append(('indirect block size', prof['indirect_block_size'],
                       'zscan assumes 16 KiB indirect/dnode blocks (BLK16K); make it a parameter.'))
    for kind, val, hint in issues:
        print(f'   NOT SUPPORTED YET: {kind} = {val}\n      -> {hint}')
    if not issues:
        print('   all detected properties are supported')

    case = {
        'name': name, 'created': time.strftime('%Y-%m-%d %H:%M:%S'),
        'pool': {'name': pool, 'guid': lab['pool_guid'], 'topology': topo, 'ashift': lab['ashift'],
                 'top_level_vdevs': lab['vdev_children'], 'version': lab['version'],
                 'features_for_read': lab['features_for_read'],
                 'devices': [{'path': l['device'], 'vdev_guid': l.get('guid')} for l in labels]},
        'profile': prof,
        'timeline': [{'txg': t, 'time': ts} for t, ts in ubs],
        'open_txg': txg, 'datasets': datasets,
        'target': {'dataset': target, 'root_names': [x for x in names.split(',') if x]},
        'compatibility': [{'what': k, 'value': v, 'hint': h} for k, v, h in issues],
        # scan-device.sh writes here by default; smart_disks (e.g. ["sda"]) adds temperatures to the status page
        'scan': {'device': os.path.basename(labels[0]['device']), 'chunk_dir': f'logs/raw/scan_{os.path.basename(labels[0]['device'])}',
                 'log': f'logs/scan_{os.path.basename(labels[0]['device'])}.log'},
        'smart_disks': [],
    }
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, f'{name}.json')
    json.dump(case, open(path, 'w'), indent=1, ensure_ascii=False, default=str)
    print(f'== written {path}')


if __name__ == '__main__':
    main()
