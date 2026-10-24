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
import signal
import subprocess
import sys
import tempfile
import time

# What zfs-salvage supports (README capability matrix). Extend here when you add support.
SUPPORTED = {
    'top_level_types': {'file', 'disk', 'mirror'},
    'compression': {'off', 'uncompressed', 'on', 'lzjb', 'lz4'},      # of the target dataset's data blocks
    'checksum': {'on', 'fletcher4', 'sha256'},
}
HINTS = {
    'zstd': 'Add zstd (ZFS uses a 4-byte header before the frame) to libzsalvage, zscan and zsread.',
    'gzip': 'Add zlib inflate (gzip-1..9) to libzsalvage, zscan and zsread.',
    'raidz': 'RAIDZ: scan each child disk and map physical to logical offsets using the raidz geometry; '
             'reads must reconstruct from data columns (and parity on errors).',
    'draid': 'dRAID is not supported; see the raidz hint plus the permutation tables.',
    'checksum': 'Add the checksum algorithm to zsread.checksum() (and zscan bp_ok if metadata uses it).',
    'large_dnode': 'large_dnode: dnodes may span several 512-byte slots (dn_extra_slots); adapt zscan/zsread.',
    'encryption': 'Native encryption: data and most metadata are encrypted; requires the key and AES-GCM/CCM.',
    'missing': 'Attach all devices of the pool (every top-level vdev) and try again.',
    'removal': 'Device removal leaves "indirect" vdevs that map old offsets to new ones; not supported.',
}


PROBLEMS = []                  # one line per zdb call that failed; written to the case file
ZDB_TIMEOUT = 120              # seconds per zdb call (--zdb-timeout)
CRASH_CODES = {134: 'SIGABRT (assertion failed)', 139: 'SIGSEGV', 136: 'SIGFPE', 132: 'SIGILL', 135: 'SIGBUS'}


def sh(cmd, timeout=None):
    """Run a command and return stdout+stderr. A missing program, a timeout or a crash (zdb aborts on
    damaged pools, older versions even crash the kernel) is recorded in PROBLEMS and answered with the
    output so far instead of an exception."""
    timeout = timeout or ZDB_TIMEOUT
    what = ' '.join(str(c) for c in cmd[:2]) + (' ' + ' '.join(str(c) for c in cmd[-2:]) if len(cmd) > 4 else '')
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        PROBLEMS.append(f'{cmd[0]} not found')
        print(f'   PROBLEM: {cmd[0]} not found')
        return ''
    except subprocess.TimeoutExpired as e:
        PROBLEMS.append(f'timeout after {timeout} s: {what}')
        print(f'   PROBLEM: no answer after {timeout} s from: {what}')
        out = e.stdout or b''
        return out.decode('utf-8', 'replace') if isinstance(out, bytes) else out
    if r.returncode < 0 or r.returncode in CRASH_CODES:
        sig = CRASH_CODES.get(r.returncode) or signal.Signals(-r.returncode).name
        if f'crashed with {sig}: {what}' not in PROBLEMS:          # say it once, not for every TXG
            print(f'   PROBLEM: {cmd[0]} crashed with {sig}: {what}')
        PROBLEMS.append(f'crashed with {sig}: {what}')
    return r.stdout + r.stderr


def ask(prompt, default, assume_yes):
    if assume_yes:
        return default
    v = input(f'{prompt} [{default}]: ').strip()
    return v or default


# ------------------------------------------------------------------ discovery
def read_only_flag(dev):
    """'1' if the device is read-only: the kernel flag of a block device, the write bit of an image file."""
    if os.path.isfile(dev):
        return '0' if os.stat(dev).st_mode & 0o200 else '1'
    return sh(['blockdev', '--getro', dev]).strip()


def find_devices():
    out = sh(['lsblk', '-pnro', 'NAME,TYPE,FSTYPE,SIZE,RO'])
    devs = []
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 3 and f[2] == 'zfs_member':
            devs.append(f[0])
    return devs


def vdev_groups(labels):
    """Top-level vdevs of the given devices: [{'id', 'type', 'devices'}], from their labels."""
    groups = {}
    for l in labels:
        if l.get('top_id') is not None:
            g = groups.setdefault(l['top_id'], {'id': l['top_id'], 'type': l.get('top_type'), 'devices': []})
            g['devices'].append(l['device'])
    return [groups[k] for k in sorted(groups)]


def read_label(zdb, dev):
    out = sh([zdb, '-l', dev])
    lab = {'device': dev}
    m = re.search(r"^\s+name: '([^']+)'", out, re.M)
    lab['pool'] = m.group(1) if m else None
    for key in ('pool_guid', 'txg', 'guid', 'top_guid', 'ashift', 'version', 'vdev_children'):
        m = re.search(rf'^\s+{key}: (\d+)', out, re.M)
        lab[key] = int(m.group(1)) if m else None
    m = re.search(r"^\s+vdev_tree:\n\s+type: '([^']+)'\n\s+id: (\d+)", out, re.M)
    lab['top_type'], lab['top_id'] = (m.group(1), int(m.group(2))) if m else (None, None)   # top-level vdev
    lab['children'] = len(set(re.findall(r'^\s+children\[(\d+)\]:', out, re.M)))     # members of the top-level vdev
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


def try_open(zdb, devdir, pool, txgs, limit=40, budget=600):
    """Newest TXGs first; returns (txg, zdb -d output) of the first TXG where the pool opens. Gives up
    after `limit` TXGs or `budget` seconds."""
    tried, t0, failed_in_a_row = [], time.time(), 0
    for txg in sorted(txgs, reverse=True)[:limit]:
        if failed_in_a_row >= 3:
            PROBLEMS.append('zdb failed on 3 TXGs in a row, not trying more')
            print('   PROBLEM: zdb failed on 3 TXGs in a row, not trying more')
            break
        if time.time() - t0 > budget:
            PROBLEMS.append(f'gave up opening the pool after {len(tried)} TXGs ({budget} s)')
            print(f'   PROBLEM: gave up opening the pool after {len(tried)} TXGs ({budget} s)')
            break
        before = len(PROBLEMS)
        out = sh([zdb, '-e', '-p', devdir, '-t', str(txg), '-d', pool])
        failed_in_a_row = failed_in_a_row + 1 if len(PROBLEMS) > before else 0
        ok = 'Dataset ' in out and "can't open" not in out.split('Dataset ')[0]
        tried.append((txg, ok))
        if ok:
            return txg, out, tried
    return None, '', tried


DATASET_RE = re.compile(r'^Dataset (\S+) \[(\w+)\], ID (\d+), cr_txg (\d+), (\S+), (\d+) objects(?:, rootbp (.*))?$', re.M)


def parse_datasets(out):
    """Datasets from `zdb -d` / `zdb -dddd <dataset> 0`. The rootbp part (checksum, compression and
    encryption of the objset block) is only printed by some zdb versions or at higher verbosity."""
    ds = []
    for m in DATASET_RE.finditer(out):
        name, typ, did, cr, size, objs, bp = m.groups()
        comp = re.search(r'\] (\w+) (\w+) (unencrypted|encrypted|authenticated)', bp or '')
        birth = re.search(r'birth=(\d+)L', bp or '')
        ds.append({'name': name, 'type': typ, 'id': int(did), 'cr_txg': int(cr), 'size': size,
                   'objects': int(objs), 'checksum': comp.group(1) if comp else None,
                   'compression': comp.group(2) if comp else None,
                   'encrypted': bool(comp and comp.group(3) != 'unencrypted'),
                   'objset_birth': int(birth.group(1)) if birth else None, 'readable': True})
    for m in re.finditer(r"failed to hold dataset '([^']+)': (.*)$", out, re.M):
        ds.append({'name': m.group(1), 'readable': False, 'error': m.group(2).strip()})
    return ds


def dataset_header(zdb, devdir, pool, txg, name):
    """The header line of one dataset (`zdb -dddd <dataset> 0`): checksum, compression and encryption of
    its objset block. {} if the dataset cannot be opened."""
    cmd = [zdb, '-e', '-p', devdir] + (['-t', str(txg)] if txg else []) + ['-dddd', name, '0']
    found = [d for d in parse_datasets(sh(cmd)) if d['name'] == name and d['readable']]
    return found[0] if found else {}


def has_large_dnodes(zdb, devdir, pool, txg, name):
    """True/False if the dataset has dnodes larger than 512 bytes (`dnsize` column of `zdb -dd`), None if
    that could not be determined (timeout, unreadable)."""
    cmd = [zdb, '-e', '-p', devdir] + (['-t', str(txg)] if txg else []) + ['-dd', name]
    try:
        out = sh(cmd, timeout=min(180, ZDB_TIMEOUT))
    except subprocess.TimeoutExpired:
        return None
    sizes = re.findall(r'^\s+\d+\s+\d+\s+\S+\s+\S+\s+\S+\s+(\S+)\s+\S+\s+\S+\s+\S', out, re.M)
    return any(x != '512' for x in sizes) if sizes else None


def pool_features(zdb, devdir, pool, txg):
    """Active pool features with their reference counts, from the MOS object `features_for_write`
    (per-dataset features such as encryption and large_dnode are not listed there)."""
    base = [zdb, '-e', '-p', devdir] + (['-t', str(txg)] if txg else [])
    m = re.search(r'features_for_write = (\d+)', sh(base + ['-dddd', pool, '1']))
    if not m:
        return {}
    out = sh(base + ['-dddd', pool, m.group(1)])
    return {k: int(v) for k, v in re.findall(r'^\s+((?:com|org)\.[\w.]+:\w+) = (\d+)', out, re.M)}


def meta_dnode_profile(zdb, devdir, pool, txg, ds_name):
    """Indirect block size and levels of the meta-dnode of a readable dataset (object 0)."""
    out = sh([zdb, '-e', '-p', devdir, '-t', str(txg), '-dddd', ds_name, '0'])
    m = re.search(r'^\s+0\s+(\d+)\s+(\S+)\s+(\S+)\s+\S+\s+(\S+)?.*DMU dnode', out, re.M)
    if not m:
        return {}
    to_bytes = lambda s: int(float(s[:-1]) * {'K': 1024, 'M': 1 << 20}[s[-1]]) if s[-1] in 'KM' else int(s)
    return {'meta_dnode_levels': int(m.group(1)), 'indirect_block_size': to_bytes(m.group(2)),
            'dnode_block_size': to_bytes(m.group(3))}


def target_info(zdb, devdir, pool, txg, ds_name):
    """Head dataset object of an unreadable dataset via the MOS (birth of its objset bp)."""
    out = sh([zdb, '-e', '-p', devdir, '-t', str(txg), '-dddd', f'{pool}/0'])
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
def check_compat(lab, labels, datasets, target, prof):
    """(problems, warnings): lists of (kind, value, sentence, hint). Problems stop make-case (exit 2).

    lab: label of one device (pool-wide fields), labels: labels of all given devices (they carry the
    top-level vdev id and type), datasets: parsed `zdb -d` output (may be empty), target: dataset name.
    """
    problems, warnings = [], []
    tgt0 = next((d for d in datasets if d['name'] == target), None)
    groups = vdev_groups(labels)
    have = {g['id'] for g in groups}
    want = set(range(lab.get('vdev_children') or len(have) or 1))

    for g in groups:
        t = g['type']
        if t in ('raidz', 'draid'):
            problems.append(('layout', t, f"top-level vdev {g['id']} is {t}, which is not supported (yet)",
                             HINTS[t]))
        elif t not in SUPPORTED['top_level_types']:
            problems.append(('layout', t, f"top-level vdev {g['id']} is of type '{t}', which is not supported",
                             'Only single devices and mirrors (also several of them) are supported.'))
    missing = sorted(want - have)
    if missing:
        special = lab.get('active_features', {}).get('org.zfsonlinux:allocation_classes', 0) > 0
        what = ('the pool uses allocation classes, so the missing vdev is probably a "special" vdev that holds '
                'the metadata; without it nothing can be found by scanning the other devices. ' if special else
                'if it was a "special" vdev (allocation classes) it held the metadata, and nothing can be '
                'found by scanning the other devices. ')
        problems.append(('devices', missing, f"top-level vdev(s) {missing} of {len(want)} have no device: {what}"
                         "Otherwise the pool is incomplete, or the vdev was removed from it", HINTS['missing']))
    for g in groups:
        total = next((l.get('children', 0) for l in labels if l.get('top_id') == g['id']), 0)
        if g['type'] == 'mirror' and total > len(g['devices']):
            warnings.append(('devices', f"{len(g['devices'])}/{total}",
                             f"mirror {g['id']}: only {len(g['devices'])} of {total} members were given, there is "
                             "one copy less to fall back on", ''))

    feats = [f.split(':')[-1] for f in lab.get('features_for_read', [])]
    if (tgt0 or {}).get('encrypted'):
        problems.append(('feature', 'encryption', 'the target dataset is natively encrypted; its files cannot be '
                         'read without the key and decryption is not supported', HINTS['encryption']))
    elif 'encryption' in feats or any(d.get('encrypted') for d in datasets):
        warnings.append(('feature', 'encryption', 'the pool has natively encrypted datasets; that is only a '
                         'problem if the dataset you want to recover is one of them', HINTS['encryption']))
    if 'device_removal' in feats:
        problems.append(('feature', 'device_removal', 'a device was removed from the pool', HINTS['removal']))
    if 'large_dnode' in feats or (tgt0 or {}).get('large_dnodes'):
        warnings.append(('feature', 'large_dnode', 'the pool uses large dnodes: file data, sizes and times are '
                         'recovered, extended attributes stored in the extra dnode space may be missing', HINTS['large_dnode']))

    tgt = next((d for d in datasets if d['name'] == target), None)
    if tgt and tgt.get('readable'):
        c, k = tgt.get('compression'), tgt.get('checksum')
        if c and c not in SUPPORTED['compression']:
            problems.append(('compression', c, f"the target dataset's data is compressed with {c}, which cannot "
                             'be decompressed; its files would be unreadable', HINTS.get(c.split('-')[0], f'add {c}')))
        if k and k not in SUPPORTED['checksum']:
            problems.append(('checksum', k, f"the target dataset's data uses the {k} checksum, which cannot be "
                             'verified', HINTS['checksum']))
    elif target:
        warnings.append(('target', target, 'the target dataset could not be read, so its compression and '
                         'checksum are unknown; zstd/gzip-compressed or unverifiable data would show up as '
                         'unreadable files during the transfer', ''))
    for d in datasets:                       # other datasets: only a note
        if d is not tgt and d.get('readable') and d.get('type') == 'ZPL' and '@' not in d['name'] \
                and (d.get('compression') not in (None, *SUPPORTED['compression'])
                     or d.get('checksum') not in (None, *SUPPORTED['checksum'])):
            warnings.append(('dataset', d['name'], f"{d['name']} uses {d.get('compression')}/{d.get('checksum')}, "
                             'part of its data cannot be read', ''))
    ind = prof.get('indirect_block_size')
    if ind not in (None, 16384, 131072):
        warnings.append(('indirect block size', ind, f'indirect blocks of {ind} bytes: zscan only tries 128 KiB '
                         'and 16 KiB by default', 'Scan with ZFS_SALVAGE_IND_SIZES="<size>" (scripts/scan-device.sh).'))
    return problems, warnings


def main():
    global ZDB_TIMEOUT
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--zdb', default='zdb')
    ap.add_argument('--dev', action='append', default=[])
    ap.add_argument('--target')
    ap.add_argument('--root-names', default='')
    ap.add_argument('--name')
    ap.add_argument('--out', default=os.environ.get('ZFS_SALVAGE_CASES_DIR') or 'cases')
    ap.add_argument('--yes', action='store_true', help='accept defaults, no questions')
    ap.add_argument('--zdb-timeout', type=int, default=ZDB_TIMEOUT, help='seconds per zdb call (default %(default)s)')
    ap.add_argument('--zdb-budget', type=int, default=600,
                    help='seconds in total for trying to open the pool at recent TXGs (default %(default)s)')
    ap.add_argument('--force', action='store_true',
                    help='write the case file even if the pool has properties that are not supported (exit code 0)')
    a = ap.parse_args()
    ZDB_TIMEOUT = a.zdb_timeout

    print('== 1. devices')
    devs = a.dev or find_devices()
    if not devs:
        sys.exit('No ZFS member devices found. Pass --dev (e.g. an opened LUKS mapper). '
                 'Open encrypted devices READ-ONLY: cryptsetup open --readonly ...')
    for d in devs:
        ro = read_only_flag(d)
        print(f'   {d}  read-only={ro}' + ('' if ro == '1' else '   <-- WARNING: not read-only (blockdev --setro / overlay recommended)'))

    print('== 2. labels')
    labels = [read_label(a.zdb, d) for d in devs]
    pool = next((l['pool'] for l in labels if l.get('pool')), None)
    if not pool:
        why = '; '.join(PROBLEMS) or 'zdb printed no label'
        sys.exit(f'No readable ZFS label on the given devices ({why}).\n'
                 f'`{a.zdb} -l <device>` has to work: install zfsutils-linux (zdb 2.2 is enough), or check that '
                 'the devices are the pool members and opened read-only.')
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
    txg, out, tried = try_open(a.zdb, devdir, pool, [t for t, _ in ubs], budget=a.zdb_budget)
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
    tgt = next((d for d in datasets if d['name'] == target and d.get('readable')), None)
    if tgt and txg:                                  # compression/checksum/encryption of its objset block
        tgt.update({k: v for k, v in dataset_header(a.zdb, devdir, pool, txg, target).items()
                    if k in ('checksum', 'compression', 'encrypted', 'objset_birth')})
        prof['encrypted'] = prof['encrypted'] or bool(tgt.get('encrypted'))
        tgt['large_dnodes'] = has_large_dnodes(a.zdb, devdir, pool, txg, target)
    feats = pool_features(a.zdb, devdir, pool, txg) if txg else {}
    lab['active_features'] = feats
    names = ask('Names you expect in its root directory (comma separated; optional, you can also pick the file system later from the ones found)',
                a.root_names, a.yes)
    name = a.name or ask('Case name', f'{pool}-{time.strftime("%Y%m%d")}', a.yes)

    print('== 7. compatibility check')
    problems, warnings = check_compat(lab, labels, datasets, target, prof)
    for kind, val, sentence, hint in warnings:
        print(f'   NOTE: {sentence}' + (f'\n      -> {hint}' if hint else ''))
    for kind, val, sentence, hint in problems:
        print(f'   NOT SUPPORTED: {sentence}' + (f'\n      -> {hint}' if hint else ''))
    if problems and not a.force:
        print(f'\n{len(problems)} problem(s) found, no case file written. '
              'Fix the cause (e.g. attach the missing devices) or run with --force to write it anyway.')
        sys.exit(2)
    if not problems:
        print('   all detected properties are supported')
    issues = problems + warnings

    case = {
        'name': name, 'created': time.strftime('%Y-%m-%d %H:%M:%S'),
        'pool': {'name': pool, 'guid': lab['pool_guid'], 'topology': topo, 'ashift': lab['ashift'],
                 'top_level_vdevs': lab['vdev_children'], 'version': lab['version'],
                 'features_for_read': lab['features_for_read'],
                 'devices': [{'path': l['device'], 'vdev_guid': l.get('guid'), 'vdev': l.get('top_id')}
                             for l in labels],
                 'vdevs': vdev_groups(labels)},
        'profile': prof,
        'timeline': [{'txg': t, 'time': ts} for t, ts in ubs],
        'open_txg': txg, 'datasets': datasets,
        'target': {'dataset': target, 'root_names': [x for x in names.split(',') if x],
                   'type': 'zvol' if any(d['name'] == target and d.get('type') == 'ZVOL' for d in datasets) else 'zpl'},
        'compatibility': [{'what': k, 'value': v, 'text': t, 'hint': h, 'blocking': (k, v, t, h) in problems}
                          for k, v, t, h in issues],
        'unsupported': [t for _, _, t, _ in problems],
        # scan-device.sh writes here by default (one scan per top-level vdev); smart_disks (e.g. ["sda"])
        # adds temperatures to the status page
        'scan': {'device': os.path.basename(labels[0]['device']),
                 'chunk_dir': f"logs/raw/scan_{os.path.basename(labels[0]['device'])}",
                 'chunk_dirs': [f"logs/raw/scan_{os.path.basename(v['devices'][0])}" for v in vdev_groups(labels)],
                 'log': f"logs/scan_{os.path.basename(labels[0]['device'])}.log"},
        'smart_disks': [],
        'problems': [f'{p} (x{n})' if n > 1 else p for p, n in
                     ((p, PROBLEMS.count(p)) for p in dict.fromkeys(PROBLEMS))],   # zdb calls that failed
    }
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, f'{name}.json')
    json.dump(case, open(path, 'w'), indent=1, ensure_ascii=False, default=str)
    print(f'== written {path}' + (' (forced; unsupported properties listed in "unsupported")' if problems else ''))


if __name__ == '__main__':
    main()
