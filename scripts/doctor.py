#!/usr/bin/env python3
"""doctor: check that this machine is ready, in plain words.

Every line is OK, WARN or FAIL with one sentence and, if something is wrong, what to do. Exit code 1 if
there is a FAIL. `--json` prints the same as a list for the web UI.

Usage: doctor.py [--json] [--no-root]
  --no-root   not being root is only a warning (to look around without sudo)

For tests, system facts can be replaced with ZFS_SALVAGE_DOCTOR_FAKE='{"euid": 1000, "mem_kb": 500000, ...}'
(keys: euid, python, mem_kb, mounts, lsblk).
"""
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
from validate_case import validate  # noqa: E402

OK, WARN, FAIL = 'OK', 'WARN', 'FAIL'
FAKE = json.loads(os.environ.get('ZFS_SALVAGE_DOCTOR_FAKE') or '{}')
RAM_DISK_FS = ('tmpfs', 'overlay', 'ramfs', 'squashfs', 'aufs')


def run(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout + r.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def remote(cmd, timeout=30):
    """Run a command on the recovery host (ssh, or here in local mode)."""
    return run(common.remote_argv(cmd, opts=('-o', 'ConnectTimeout=8', '-o', 'BatchMode=yes')), timeout)


def mount_of(path):
    """(mount point, file system type) of the mount that holds path."""
    if 'mounts' in FAKE:
        return FAKE['mounts'].get(path) or tuple(next(iter(FAKE['mounts'].values())))
    real = os.path.realpath(path)
    best = ('/', '?')
    with open('/proc/mounts') as f:
        for line in f:
            dev, mp, fs = line.split()[:3]
            mp = mp.replace('\\040', ' ')
            if (real == mp or real.startswith(mp.rstrip('/') + '/')) and len(mp) >= len(best[0]):
                best = (mp, fs)
    return best


def case_devices():
    return [d['path'].split('@')[0] for d in common.CASE.get('pool', {}).get('devices', [])]


def member_devices():
    """Devices of the case, or, without a case, every ZFS member the system sees."""
    if case_devices():
        return case_devices(), 'case'
    out = FAKE.get('lsblk') or run(['lsblk', '-pnro', 'NAME,FSTYPE'])[1]
    return [l.split()[0] for l in out.splitlines() if len(l.split()) >= 2 and l.split()[1] == 'zfs_member'], 'lsblk'


def read_only(dev):
    """True if the device is read-only (blockdev --getro) or an image file without owner write bit."""
    if os.path.isfile(dev):
        return not os.stat(dev).st_mode & 0o200
    return run(['blockdev', '--getro', dev])[1].strip() == '1'


# ---------------------------------------------------------------- checks: each returns (status, sentence, hint)
def check_root(args):
    euid = FAKE.get('euid', os.geteuid())
    if euid == 0:
        return OK, 'running as root', ''
    return (WARN if args.no_root else FAIL), 'not running as root: the pool devices cannot be read', 'Start with: sudo ./zfs-salvage'


def check_python(args):
    v = tuple(FAKE.get('python', sys.version_info[:3]))
    if v >= (3, 8):
        return OK, f'python {".".join(map(str, v))}', ''
    return FAIL, f'python {".".join(map(str, v))} is too old (3.8 or newer is needed)', 'Use a newer live system (Ubuntu 24.04 has 3.12).'


def check_zdb(args):
    zdb = os.environ.get('ZFS_SALVAGE_ZDB') or 'zdb'
    path = shutil.which(zdb)
    if not path:
        return FAIL, 'zdb is not installed (it reads the labels of the pool devices)', 'Install it: sudo apt install zfsutils-linux'
    devs, _ = member_devices()
    if not devs:
        return WARN, f'zdb found ({path}), but no ZFS member device to test it on', 'Attach the pool disks and run doctor again.'
    rc, out = run([path, '-l', devs[0]])
    if "name: '" not in out:
        return FAIL, f'`zdb -l {devs[0]}` shows no ZFS label: {out.strip().splitlines()[0][:100] if out.strip() else "no output"}', \
            'Is that the pool member (the partition, an opened LUKS mapper), and does the user running doctor have access?'
    return OK, f'zdb works ({path}); label of {devs[0]} readable', ''


def check_zscan(args):
    z = os.environ.get('ZFS_SALVAGE_ZSCAN') or common.ZSCAN
    if common.HOST != 'local' and not os.environ.get('ZFS_SALVAGE_ZSCAN'):
        rc, out = remote('zscan --max-vdev 0 /dev/null 2>&1 | head -1; command -v zscan >/dev/null || echo MISSING')
        if rc == 255:
            return FAIL, f'cannot check zscan: {common.HOST} is not reachable over ssh', 'See the check "recovery host".'
        if 'MISSING' in out:
            return FAIL, f'zscan is not installed on {common.HOST}', 'Run scripts/deploy-tools.sh'
        if 'unknown or incomplete option' in out or 'No such file' in out:
            return FAIL, f'zscan on {common.HOST} is too old', 'Run scripts/deploy-tools.sh'
        return OK, f'zscan is installed on {common.HOST}', ''
    if not os.path.isfile(z) or not os.access(z, os.X_OK):
        return FAIL, f'zscan is not built ({z})', 'Use a release bundle, or build it: tools/zscan/build.sh (needs Docker)'
    rc, out = run([z, '--max-vdev', '0', '/dev/null'])
    if 'unknown or incomplete option' in out:
        return FAIL, 'zscan is too old (no --max-vdev)', 'Rebuild it: tools/zscan/build.sh'
    if rc not in (0, 1):
        return FAIL, f'zscan does not run here (exit {rc}): {out.strip()[:100]}', 'Rebuild it, or use the release bundle for this CPU.'
    return OK, 'zscan runs', ''


def check_lib(args):
    paths = FAKE.get('lib_paths')
    if paths is None:
        here = os.path.dirname(os.path.abspath(__file__))
        paths = [f'{here}/libzsalvage.so', f'{os.path.dirname(here)}/tools/libzsalvage/libzsalvage.so', '/usr/local/lib/libzsalvage.so']
    errors = []
    for p in paths:
        if os.path.exists(p):
            try:
                ctypes.CDLL(p)
                return OK, f'libzsalvage.so loads ({p})', ''
            except OSError as e:
                errors.append(f'{p}: {e}')
    why = errors[0] if errors else 'libzsalvage.so not found'
    return WARN, f'libzsalvage.so cannot be used ({why}): reading works but is much slower', 'Use the release bundle, or build it: tools/libzsalvage/build.sh'


def check_data_dir(args):
    d = common.STATE
    os.makedirs(d, exist_ok=True)
    mp, fs = mount_of(d)
    if fs in RAM_DISK_FS:
        return FAIL, f'the data directory {d} is in RAM ({fs}): scan results and the catalog would be lost on a reboot, and RAM is needed for the system', \
            'Unpack the bundle on a real disk (not the live system itself) and start it from there.'
    free = FAKE.get('free_bytes', shutil.disk_usage(d).free)
    if free < 1 << 30:
        return FAIL, f'only {free / 1e6:.0f} MB free in {d} (at least 1 GB is needed)', 'Free some space or use another disk.'
    return OK, f'data directory {d} on {fs}, {free / 1e9:.1f} GB free', ''


def check_devices(args):
    devs, source = member_devices()
    if not devs:
        return WARN, 'no ZFS member devices found', 'Attach the pool disks (and open encrypted ones read-only: cryptsetup open --readonly …).'
    bad = [d for d in devs if not os.path.exists(d)]
    if bad:
        return FAIL, f'device(s) of the case do not exist here: {", ".join(bad)}', 'Attach / open them, or set ZFS_SALVAGE_HOST to the machine that has them.' \
            if source == 'case' else 'Attach them.'
    writable = [d for d in devs if not read_only(d)]
    if writable:
        return FAIL, f'not read-only: {", ".join(writable)}', f'Make them read-only first: sudo blockdev --setro {writable[0]}   (image files: chmod a-w)'
    return OK, f'{len(devs)} device(s), all read-only: {", ".join(devs)}', ''


def check_case(args):
    if common.CASE == {}:
        return WARN, 'no case file yet', f'The setup step creates it (scripts/make-case.py, writes to {common.CASES}).'
    problems = validate(common.CASE)
    if problems:
        return FAIL, f'the case file is not valid: {problems[0]}' + (f' (+{len(problems) - 1} more)' if len(problems) > 1 else ''), \
            'Create it again with scripts/make-case.py.'
    return OK, f'case {common.CASE.get("name")} is valid', ''


def check_features(args):
    blocking = [c for c in common.CASE.get('compatibility', []) if c.get('blocking')]
    forced = common.CASE.get('unsupported') or []
    if blocking or forced:
        text = (blocking[0].get('text') if blocking else forced[0])
        return FAIL, f'this pool has properties that are not supported: {text}', 'Other tools (e.g. PhotoRec) can carve files by content.'
    if not common.CASE:
        return OK, 'no case yet, nothing to check', ''
    return OK, 'the pool uses only supported properties', ''


def check_ram(args):
    kb = FAKE.get('mem_kb')
    if kb is None:
        with open('/proc/meminfo') as f:
            kb = int(re.search(r'MemTotal:\s+(\d+)', f.read()).group(1))
    if kb < 2 * 1024 * 1024:
        return WARN, f'only {kb / 1024 / 1024:.1f} GB RAM (2 GB or more is recommended)', 'Close other programs; very large pools need more.'
    return OK, f'{kb / 1024 / 1024:.1f} GB RAM', ''


def check_host(args):
    if common.HOST == 'local':
        return OK, 'everything runs on this machine (no ssh needed)', ''
    rc, out = run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', common.HOST, 'true'])
    if rc != 0:
        return FAIL, f'cannot reach {common.HOST} over ssh without a password: {(out.strip().splitlines() or ["?"])[0][:100]}', \
            f'Set up a key login (ssh-copy-id {common.HOST}) or set ZFS_SALVAGE_HOST=local when the disks are on this machine.'
    return OK, f'{common.HOST} reachable over ssh', ''


CHECKS = [('root', check_root), ('python', check_python), ('zdb', check_zdb), ('zscan', check_zscan),
          ('libzsalvage', check_lib), ('data directory', check_data_dir), ('devices', check_devices),
          ('case file', check_case), ('pool features', check_features), ('memory', check_ram), ('recovery host', check_host)]


def run_checks(args):
    results = []
    for name, fn in CHECKS:
        try:
            status, text, hint = fn(args)
        except Exception as e:  # noqa: BLE001 - a broken check must not hide the others
            status, text, hint = FAIL, f'the check itself failed: {type(e).__name__}: {e}', 'Please report this.'
        results.append({'check': name, 'status': status, 'text': text, 'hint': hint})
    return results


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv

    class A:
        no_root = '--no-root' in argv
    results = run_checks(A)
    if '--json' in argv:
        print(json.dumps(results, ensure_ascii=False))
    else:
        for r in results:
            print(f'[{r["status"]:^4}] {r["check"]}: {r["text"]}' + (f'\n         -> {r["hint"]}' if r['hint'] and r['status'] != OK else ''))
        n = {s: sum(1 for r in results if r['status'] == s) for s in (OK, WARN, FAIL)}
        print(f'\n{n[OK]} ok, {n[WARN]} warnings, {n[FAIL]} problems' + (' – fix the problems above first.' if n[FAIL] else '.'))
    return 1 if any(r['status'] == FAIL for r in results) else 0


if __name__ == '__main__':
    sys.exit(main())
