#!/usr/bin/env python3
"""doctor: check that this machine is ready, in plain words.

Every line is OK, WARN or FAIL with one sentence and, if something is wrong, what to do. Exit code 1 if
there is a FAIL. `--json` prints the same as a list for the web UI.

Usage: doctor.py [--json] [--no-root] [--smart <device>]
  --no-root   not being root is only a warning (to look around without sudo)
  --smart D   only the SMART check of device D; exit 3 if its disk is failing (used by `./zfs-salvage scan`)

For tests, system facts can be replaced with ZFS_SALVAGE_DOCTOR_FAKE='{"euid": 1000, "mem_kb": 500000, ...}'
(keys: euid, python, mem_kb, mounts, lsblk, disks = {device: [physical disks]}, pool_names, services = {unit: state}).
ZFS_SALVAGE_SMARTCTL, ZFS_SALVAGE_ZPOOL and ZFS_SALVAGE_ZPOOL_CACHE replace smartctl, zpool and the cache file path.
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


def mount_info(path):
    """(mount point, file system type, source device, options) of the mount that holds path."""
    if 'mounts' in FAKE:
        mp, fs = FAKE['mounts'].get(path) or next(iter(FAKE['mounts'].values()))
        return mp, fs, FAKE.get('sources', {}).get(path, ''), FAKE.get('mount_options', {}).get(path, '')
    real = os.path.realpath(path)
    best = ('/', '?', '', '')
    with open('/proc/mounts') as f:
        for line in f:
            dev, mp, fs, opts = line.split()[:4]
            mp = mp.replace('\\040', ' ')
            if (real == mp or real.startswith(mp.rstrip('/') + '/')) and len(mp) >= len(best[0]):
                best = (mp, fs, dev, opts)
    return best


def mount_of(path):
    """(mount point, file system type) of the mount that holds path."""
    return mount_info(path)[:2]


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


def loop_backing(dev):
    """The file behind /dev/loopN, or None."""
    if dev in FAKE.get('loop_backing', {}):
        return FAKE['loop_backing'][dev]
    try:
        with open(f'/sys/block/{os.path.basename(dev)}/loop/backing_file') as f:
            return f.read().strip() or None
    except OSError:
        return None


def disks_of(dev, _depth=0):
    """The physical disks below a device (partition, LUKS mapper, md, loop device): from lsblk's inverse tree; a loop
    device belongs to the disk its backing file lives on. Image files have none."""
    if dev in FAKE.get('disks', {}):
        return FAKE['disks'][dev]
    if dev in FAKE.get('loop_backing', {}) or (_depth < 3 and not os.path.isfile(dev) and os.path.basename(dev).startswith('loop')):
        back = loop_backing(dev)
        return disks_of(mount_info(back)[2], _depth + 1) if back and _depth < 3 else []
    if os.path.isfile(dev) or 'disks' in FAKE:
        return []
    out = run(['lsblk', '-psnro', 'NAME,TYPE', dev])[1]
    disks = []
    for name, kind in (l.split()[:2] for l in out.splitlines() if len(l.split()) >= 2):
        if kind == 'disk':
            disks.append(name)
        elif kind == 'loop' and _depth < 3:
            disks += disks_of(name, _depth + 1)
    return disks


def smart_problems(disk):
    """What smartctl says about a disk: (problems, error). problems is a list of plain-word findings, empty if healthy."""
    smartctl = os.environ.get('ZFS_SALVAGE_SMARTCTL') or shutil.which('smartctl')
    if not smartctl:
        return [], 'smartctl is not installed'
    rc, out = run([smartctl, '-j', '-H', '-A', disk])
    try:
        j = json.loads(out)
    except ValueError:
        return [], f'smartctl gave no answer for {disk}'
    problems = []
    if j.get('smart_status', {}).get('passed') is False:
        problems.append('the health check failed')
    attrs = {a.get('id'): a.get('raw', {}).get('value', 0) for a in j.get('ata_smart_attributes', {}).get('table', [])}
    for ident, what in ((5, 'reallocated sectors'), (197, 'sectors waiting to be reallocated'), (198, 'uncorrectable sectors')):
        if attrs.get(ident):
            problems.append(f'{attrs[ident]} {what}')
    nvme = j.get('nvme_smart_health_information_log', {})
    if nvme.get('media_errors'):
        problems.append(f'{nvme["media_errors"]} media errors')
    if nvme.get('critical_warning'):
        problems.append('a critical warning')
    return problems, None


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
        return FAIL, f'device(s) of the case do not exist here: {", ".join(bad)}', 'Attach / open them on this machine (zfs-salvage runs where the disks are).' \
            if source == 'case' else 'Attach them.'
    writable = [d for d in devs if not read_only(d)]
    if writable:
        return FAIL, f'not read-only: {", ".join(writable)}', f'Make them read-only first: sudo blockdev --setro {writable[0]}   (image files: chmod a-w)'
    return OK, f'{len(devs)} device(s), all read-only: {", ".join(devs)}', ''


def check_smart(args):
    devs, _ = member_devices()
    disks = sorted({d for dev in devs for d in disks_of(dev)})
    if not disks:
        return OK, 'no physical disks to ask (image files, or no pool devices yet)', ''
    failing, unknown = [], None
    for d in disks:
        problems, err = smart_problems(d)
        if err:
            unknown = unknown or err
        elif problems:
            failing.append(f'{d}: {", ".join(problems)}')
    if failing:
        return WARN, f'This disk is failing – {"; ".join(failing)}', \
            'Make an image with ddrescue first and work on the image (see the README, "Failing disks").'
    if unknown:
        return WARN, f'{unknown}: nobody can tell whether the disks are healthy', 'Install it: sudo apt install smartmontools'
    return OK, f'SMART looks healthy ({", ".join(disks)})', ''


ZPOOL_CACHE = os.environ.get('ZFS_SALVAGE_ZPOOL_CACHE') or '/etc/zfs/zpool.cache'


def pool_names():
    """Names of the pool(s) we are about to rescue: the case's, else the ones in the labels of the member devices."""
    if 'pool_names' in FAKE:
        return FAKE['pool_names']
    if common.CASE.get('pool', {}).get('name'):
        return [common.CASE['pool']['name']]
    zdb = shutil.which(os.environ.get('ZFS_SALVAGE_ZDB') or 'zdb')
    names = []
    for dev in (member_devices()[0][:4] if zdb else []):
        m = re.search(r"^\s+name: '([^']+)'", run([zdb, '-l', dev])[1], re.M)
        if m and m.group(1) not in names:
            names.append(m.group(1))
    return names


def service_state(unit):
    if unit in FAKE.get('services', {}):
        return FAKE['services'][unit]
    return run(['systemctl', 'is-enabled', unit])[1].strip() if shutil.which('systemctl') else ''


def check_import(args):
    """The pool must not be imported, and nothing may import it behind our back: an import writes to the disks."""
    names = pool_names()
    if not names:
        return OK, 'no pool name known yet, nothing to check', ''
    zpool = os.environ.get('ZFS_SALVAGE_ZPOOL') or shutil.which('zpool')
    imported = set()
    if zpool:
        rc, out = run([zpool, 'list', '-H', '-o', 'name'])
        imported = set(out.split()) if rc == 0 else set()
    for name in names:
        if name in imported:
            return FAIL, f'the pool {name} is imported on this machine, and an import writes to the disks', \
                f'Stop that first: sudo zpool export -f {name}   (then check again; better still, work on images of the disks)'
    try:
        with open(ZPOOL_CACHE, 'rb') as f:
            cached = f.read()
    except OSError:
        cached = b''
    for name in names:
        if name.encode() in cached:
            return FAIL, f'the pool {name} is listed in {ZPOOL_CACHE}, so ZFS imports it the next time it starts', \
                f'Move the file away: sudo mv {ZPOOL_CACHE} {ZPOOL_CACHE}.off'
    if service_state('zfs-import-scan.service') == 'enabled':
        return WARN, 'zfs-import-scan.service is enabled: at the next start ZFS imports every pool it finds', \
            'Turn it off while you work on this pool: sudo systemctl disable zfs-import-scan.service'
    return OK, f'{", ".join(names)} is not imported and nothing is set up to import it', ''


def check_case(args):
    if common.CASE == {}:
        return WARN, 'no case file yet', 'Normal at the start: step 0 "Set up" of the web UI creates it (or ./zfs-salvage make-case).'
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
        return FAIL, f'this pool has properties that are not supported: {text}', 'See the capability matrix in the README; other tools (e.g. PhotoRec) can carve files by content.'
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


CHECKS = [('root', check_root), ('python', check_python), ('zdb', check_zdb), ('zscan', check_zscan),
          ('libzsalvage', check_lib), ('data directory', check_data_dir), ('devices', check_devices),
          ('import', check_import), ('smart', check_smart), ('case file', check_case), ('pool features', check_features), ('memory', check_ram)]


def run_checks(args):
    results = []
    for name, fn in CHECKS:
        try:
            status, text, hint = fn(args)
        except Exception as e:  # noqa: BLE001 - a broken check must not hide the others
            status, text, hint = FAIL, f'the check itself failed: {type(e).__name__}: {e}', 'Please report this.'
        results.append({'check': name, 'status': status, 'text': text, 'hint': hint})
    return results


def smart_only(dev):
    """For `./zfs-salvage scan`: say whether the disk below dev is failing. Exit 3 if so, else 0."""
    failing = []
    for d in disks_of(dev):
        problems, _ = smart_problems(d)
        if problems:
            failing.append(f'{d}: {", ".join(problems)}')
    if failing:
        print(f'This disk is failing – {"; ".join(failing)}.\nEvery pass over it costs life. Make an image with ddrescue first '
              'and work on the image (README, "Failing disks").')
        return 3
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if '--smart' in argv:
        return smart_only(argv[argv.index('--smart') + 1])

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
