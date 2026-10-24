#!/usr/bin/env python3
"""make-case - inspect a damaged ZFS pool (read-only) and write a case file for zfs-salvage.

Runs on the recovery host. Uses `zdb -l`/`-luuuu` (labels and uberblocks: they never open the pool) and
`zsread.py datasets` (our own reader walks the pool's metadata); only reads from the devices, nothing is imported. Everything that is specific to one pool ends up in the case file;
the other tools read it instead of hardcoding assumptions.

Steps: find devices -> read labels -> uberblock timeline -> read the datasets from the newest
uberblock whose metadata verifies (readable / unreadable) -> derive the on-disk profile ->
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
import time

# What zfs-salvage supports (README capability matrix). Extend here when you add support.
SUPPORTED = {
    'top_level_types': {'file', 'disk', 'mirror'},
    'compression': {'off', 'uncompressed', 'on', 'lzjb', 'lz4'},      # of the target dataset's data blocks
    'checksum': {'on', 'fletcher4', 'sha256'},
}
# What the tool says when it cannot help: (what is wrong, what to do instead). Plain words; {x} are filled in.
# The README's troubleshooting table repeats the fixed parts (tests/test_readme.py checks that).
REFUSALS = {
    'raidz': ('top-level vdev {id} is {type}, which zfs-salvage cannot read yet',
              'Support for raidz is planned. Until then, file carving by content (e.g. PhotoRec) on the '
              'assembled disks may find some files. Do not try to import a damaged pool.'),
    'raidz-degraded': ('raidz vdev {id} has only {have} of {total} disks here, and rebuilding a missing disk from parity is not supported',
                       'Attach the missing disk if you can. Otherwise file carving by content (e.g. PhotoRec) is the alternative.'),
    'layout': ("top-level vdev {id} is of type '{type}', which is not supported",
               'Single disks, mirrors and stripes of them are supported. File carving by content (e.g. PhotoRec) works on any layout.'),
    'missing': ('top-level vdev(s) {missing} of {want} have no device',
                'Attach the missing disks and run again. If a missing vdev is a "special" vdev it held the metadata of the pool: '
                'then the directory structure is gone with it, and only file carving by content (e.g. PhotoRec) can find files.'),
    'encryption': ('the target dataset is natively encrypted: its files cannot be read without the keys, and decrypting is not supported',
                   'With the key, use ZFS itself on an image of the disks (zfs load-key). Without it, nothing can read the files, '
                   'and carving finds only encrypted noise.'),
    'removal': ('a device was removed from the pool, so some blocks live at moved addresses that zfs-salvage cannot follow',
                'Use ZFS itself on an image of the disks if the pool still opens.'),
    'compression': ("the target dataset's data is compressed with {what}, which zfs-salvage cannot decompress yet: its files would be unreadable",
                    'Support for zstd and gzip is planned. If the pool still opens in a current OpenZFS, use ZFS '
                    'itself on an image of the disks.'),
    'checksum': ("the target dataset's data uses the {what} checksum, which zfs-salvage cannot verify yet",
                 'Support is planned. If the pool still opens in a current OpenZFS, use ZFS itself on an image of the disks.'),
}


def refusal(key, **kw):
    sentence, hint = REFUSALS[key]
    return sentence.format(**kw), hint



PROBLEMS = []                  # one line per zdb call that failed; written to the case file
ZDB_TIMEOUT = 120              # seconds per zdb call (--zdb-timeout)
CRASH_CODES = {134: 'SIGABRT (assertion failed)', 139: 'SIGSEGV', 136: 'SIGFPE', 132: 'SIGILL', 135: 'SIGBUS'}


def sh(cmd, timeout=None, env=None):
    """Run a command and return stdout+stderr. A missing program, a timeout or a crash (zdb aborts on
    damaged pools, older versions even crash the kernel) is recorded in PROBLEMS and answered with the
    output so far instead of an exception."""
    timeout = timeout or ZDB_TIMEOUT
    what = ' '.join(str(c) for c in cmd[:2]) + (' ' + ' '.join(str(c) for c in cmd[-2:]) if len(cmd) > 4 else '')
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
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
    try:
        v = input(f'{prompt} [{default}]: ').strip()
    except EOFError:                          # no terminal (started from a script): take the proposed answer
        print()
        return default
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
    """(txg, timestamp) of all uberblocks of the given devices, oldest first, and {txg: text of its root block
    pointer}. `zdb -luuuu` reads the labels only; it does not open the pool."""
    ubs, rootbps = {}, {}
    for d in devs:
        out = sh([zdb, '-luuuu', d])
        txg = None
        for line in out.splitlines():
            m = re.search(r'\btxg = (\d+)', line)
            if m:
                txg = int(m.group(1))
            m = re.search(r'timestamp = (\d+)', line)
            if m and txg is not None:
                ubs.setdefault(txg, int(m.group(1)))
            m = re.search(r'\brootbp = (DVA\[.*)$', line)
            if m and txg is not None:
                rootbps.setdefault(txg, m.group(1))
    return sorted(ubs.items()), rootbps


ZSREAD = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'zsread.py')


def zsread_datasets(labels, pool, rootbps, timeout, large_dnodes=()):
    """Run `zsread.py datasets` (it walks MOS -> DSL directories itself, nothing is imported): newest root block
    first, the first whose MOS verifies is used. Returns the parsed answer, or None if zsread failed."""
    groups = vdev_groups(labels)
    env = dict(os.environ, ZSREAD_LABEL_START=str(4 << 20))
    if groups and all(l.get('top_id') is not None for l in labels):
        env['ZSREAD_VDEVS'] = ';'.join(f"{g['id']}={','.join(g['devices'])}" for g in groups)
    else:
        env['ZSREAD_DEVS'] = ':'.join(l['device'] for l in labels)
    cmd = [sys.executable, '-I', ZSREAD, 'datasets', *sum([['--large-dnodes', n] for n in large_dnodes], []), pool, *rootbps]
    out = sh(cmd, timeout=timeout, env=env)
    for line in out.splitlines():
        if line.startswith('{'):
            try:
                return json.loads(line)
            except ValueError:
                pass
    PROBLEMS.append(f'zsread datasets gave no answer: {out.strip()[-200:] or "no output"}')
    return None


# ------------------------------------------------------------------ diagnosis
def diagnose(ubs, txg, datasets, target):
    """Where the way from the uberblock to the target's files is broken, judged by what was actually read:
    situation 'mos' (the pool's metadata cannot be read), 'missing' (the target is not in the pool's list: destroyed, or
    a wrong name), 'objset' (listed, but its objset block cannot be read) or 'intact' (it opens normally)."""
    tgt = next((d for d in datasets if d['name'] == target), None)
    d = {'uberblock_txg': ubs[-1][0] if ubs else None, 'mos_txg': txg, 'dataset_listed': tgt is not None,
         'objset_readable': bool(tgt and tgt.get('readable')), 'objset_birth': (tgt or {}).get('objset_birth'),
         'objset_error': (tgt or {}).get('error')}
    if txg is None:
        d['situation'] = 'mos'
        d['text'] = ("The pool's own metadata (MOS) cannot be read at any uberblock, so not even the list of datasets is available. "
                     'The scan can still find file system trees directly on the disk.')
    elif tgt is None:
        d['situation'] = 'missing'
        d['text'] = (f"{target} is not in the pool's list of datasets: it was destroyed, or the name is wrong. If it was destroyed, "
                     'its old trees may still be on the disk; the scan finds them.')
    elif not tgt.get('readable'):
        d['situation'] = 'objset'
        d['text'] = (f'{target} is listed, but its objset block cannot be read ({tgt.get("error", "no valid copy")}). That is the damage '
                     'this tool works around: the scan finds the file tree directly on the disk.')
    else:
        d['situation'] = 'intact'
        d['text'] = (f'{target} opens normally: nothing is broken on the way to its files. Use this tool for files that were deleted '
                     'later (an older state), or just use ZFS itself.')
    return d


# ------------------------------------------------------------------ main
def check_compat(lab, labels, datasets, target, prof):
    """(problems, warnings): lists of (kind, value, sentence, hint). Problems stop make-case (exit 2).

    lab: label of one device (pool-wide fields), labels: labels of all given devices (they carry the
    top-level vdev id and type), datasets: from `zsread.py datasets` (may be empty), target: dataset name.
    """
    problems, warnings = [], []
    tgt0 = next((d for d in datasets if d['name'] == target), None)
    groups = vdev_groups(labels)
    have = {g['id'] for g in groups}
    want = set(range(lab.get('vdev_children') or len(have) or 1))

    for g in groups:
        t = g['type']
        total = next((l.get('children', 0) for l in labels if l.get('top_id') == g['id']), 0)
        if t in ('raidz', 'draid'):
            problems.append(('layout', t, *refusal('raidz', id=g['id'], type=t)))
            if total > len(g['devices']):
                problems.append(('layout', f"{t}-degraded", *refusal('raidz-degraded', id=g['id'], have=len(g['devices']), total=total)))
        elif t not in SUPPORTED['top_level_types']:
            problems.append(('layout', t, *refusal('layout', id=g['id'], type=t)))
    missing = sorted(want - have)
    if missing:
        special = lab.get('active_features', {}).get('org.zfsonlinux:allocation_classes', 0) > 0
        sentence, hint = refusal('missing', missing=missing, want=len(want))
        if special:
            sentence += ' (the pool uses allocation classes: the missing vdev is probably a "special" vdev that holds the metadata)'
        problems.append(('devices', missing, sentence, hint))
    for g in groups:
        total = next((l.get('children', 0) for l in labels if l.get('top_id') == g['id']), 0)
        if g['type'] == 'mirror' and total > len(g['devices']):
            warnings.append(('devices', f"{len(g['devices'])}/{total}",
                             f"mirror {g['id']}: only {len(g['devices'])} of {total} members were given, there is "
                             "one copy less to fall back on", ''))

    feats = [f.split(':')[-1] for f in lab.get('features_for_read', [])]
    if (tgt0 or {}).get('encrypted'):
        problems.append(('feature', 'encryption', *refusal('encryption')))
    elif 'encryption' in feats or any(d.get('encrypted') for d in datasets):
        warnings.append(('feature', 'encryption', 'the pool has natively encrypted datasets; that is only a '
                         'problem if the dataset you want to recover is one of them', ''))
    if 'device_removal' in feats:
        problems.append(('feature', 'device_removal', *refusal('removal')))
    if 'large_dnode' in feats or (tgt0 or {}).get('large_dnodes'):
        warnings.append(('feature', 'large_dnode', 'the pool uses large dnodes: file data, sizes and times are '
                         'recovered, extended attributes stored in the extra dnode space may be missing', ''))

    tgt = next((d for d in datasets if d['name'] == target), None)
    if tgt and tgt.get('readable'):
        c, k = tgt.get('compression'), tgt.get('checksum')
        if c and c not in SUPPORTED['compression']:
            problems.append(('compression', c, *refusal('compression', what=c)))
        if k and k not in SUPPORTED['checksum']:
            problems.append(('checksum', k, *refusal('checksum', what=k)))
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
                    help='seconds allowed for reading the datasets from the metadata (default %(default)s)')
    ap.add_argument('--force', action='store_true',
                    help='write the case file even if the pool has properties that are not supported (exit code 0)')
    ap.add_argument('--probe-json', metavar='FILE',
                    help='only look at the pool (steps 1-5) and write what was found to FILE as JSON; no questions, no case file')
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
    ubs, rootbps = read_uberblocks(a.zdb, devs)
    if ubs:
        fmt = lambda t: time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(t))
        print(f'   {len(ubs)} uberblocks, txg {ubs[0][0]} ({fmt(ubs[0][1])} UTC) .. {ubs[-1][0]} ({fmt(ubs[-1][1])} UTC)')

    print('== 4. reading the pool\'s datasets (walking the metadata ourselves, nothing is imported)')
    newest = [rootbps[t] for t, _ in sorted(ubs, reverse=True) if t in rootbps][:40]
    found = zsread_datasets(labels, pool, newest, a.zdb_budget) if newest else None
    txg = None
    if found and found.get('rootbp_index') is not None:
        txg = sorted((t for t, _ in ubs if t in rootbps), reverse=True)[found['rootbp_index']]
    datasets = found['datasets'] if txg else []
    feats = found['features_for_write'] if txg else {}
    for err in (found or {}).get('errors', []):
        PROBLEMS.append(err)
        print(f'   PROBLEM: {err}')
    print(f'   metadata readable at txg {txg}' if txg else f'   no uberblock\'s metadata is readable ({len(newest)} tried; '
          'scanning the disk is still possible; profile will use defaults)')
    unreadable = [d for d in datasets if not d['readable']]
    readable = [d for d in datasets if d['readable'] and d.get('type') == 'ZPL']
    for d in unreadable:
        print(f"   UNREADABLE {d['name']}: {d['error']}")

    print('== 5. on-disk profile')
    comps = sorted({d['compression'] for d in datasets if d.get('compression')}) or ['unknown']
    sums = sorted({d['checksum'] for d in datasets if d.get('checksum')}) or ['unknown']
    prof = {'compression': comps, 'checksum': sums, 'label_start': 4 << 20,
            'ashift': lab['ashift'], 'encrypted': any(d.get('encrypted') for d in datasets)}
    if readable:
        prof.update({'meta_dnode_levels': readable[0]['meta_dnode']['nlevels'],
                     'indirect_block_size': readable[0]['meta_dnode']['indirect_block_size'],
                     'dnode_block_size': readable[0]['meta_dnode']['dnode_block_size']})
    print(f'   {prof}')
    if a.probe_json:                         # for the setup page: the user picks the target from this
        summary = {'pool': pool, 'topology': topo, 'ashift': lab['ashift'], 'devices': devs, 'vdevs': vdev_groups(labels),
                   'open_txg': txg, 'uberblocks': len(ubs), 'newest_txg': ubs[-1][0] if ubs else None,
                   'datasets': [{k: d.get(k) for k in ('name', 'type', 'readable', 'error', 'objects', 'size')} for d in datasets],
                   'proposed_target': unreadable[0]['name'] if len(unreadable) == 1 else None, 'problems': PROBLEMS}
        with open(a.probe_json + '.tmp', 'w') as f:
            json.dump(summary, f, ensure_ascii=False)
        os.replace(a.probe_json + '.tmp', a.probe_json)
        print(f'== written {a.probe_json}')
        return

    print('== 6. target dataset')
    if not a.target and len(unreadable) > 1:                      # several candidates: do not guess
        print(f'   {len(unreadable)} datasets cannot be read:')
        for d in unreadable:
            print(f"     {d['name']}")
        if a.yes:
            sys.exit('Several datasets cannot be read: say which one to recover with --target <name>.')
        print('   Type the one you want to recover.')
    default_target = a.target or (unreadable[0]['name'] if unreadable else (readable[0]['name'] if readable else pool))
    target = ask('Dataset to recover', default_target, a.yes)
    diagnosis = diagnose(ubs, txg, datasets, target)
    print(f"   Diagnosis: {diagnosis['text']}")
    tgt = next((d for d in datasets if d['name'] == target and d.get('readable')), None)
    if tgt:                                          # only the target is checked for large dnodes (reads its whole meta-dnode)
        again = zsread_datasets(labels, pool, newest[found['rootbp_index']:][:1], a.zdb_budget, large_dnodes=[target])
        tgt['large_dnodes'] = next((d.get('large_dnodes') for d in (again or {}).get('datasets', []) if d['name'] == target), None)
        prof['encrypted'] = prof['encrypted'] or bool(tgt.get('encrypted'))
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
        'open_txg': txg, 'datasets': datasets, 'diagnosis': diagnosis,
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
