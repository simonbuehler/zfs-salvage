"""Shared settings for the workstation-side scripts.

Everything pool-specific comes from the case file (cases/<name>.json, written by make-case.py;
pick one with ZFS_SALVAGE_CASE=<path> if there are several). The machine that has the pool's devices
is the "recovery host": ZFS_SALVAGE_HOST=<ssh alias or user@host>, or `local` when the tool runs on that
machine itself (the default if all devices of the case exist here, else `recovery-host`).
"""
import glob
import json
import os
import re
import shlex
import subprocess
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# runtime state (status page files, run logs) lives below ZFS_SALVAGE_STATE_DIR, default: the repository
STATE = os.environ.get('ZFS_SALVAGE_STATE_DIR') or REPO
STATUS = f'{STATE}/status'
LOGS = f'{STATE}/logs'
CANDIDATES = f'{STATUS}/target_candidates.json'
PROBED = f'{STATUS}/probed.jsonl'
PROBE_STATS = f'{STATUS}/probe_stats.json'
WORKFLOW = f'{STATUS}/workflow.json'
TREE_PROFILE = f'{STATUS}/tree_profile.json'     # facts measured on a verified tree (rank-trees.py)
CATALOG = os.environ.get('ZFS_SALVAGE_CATALOG_DIR') or f'{REPO}/catalog'     # catalog, selection, transfers
DB = f'{CATALOG}/catalog.sqlite'


def load_case():
    path = os.environ.get('ZFS_SALVAGE_CASE') or next(
        (p for p in sorted(glob.glob(f'{REPO}/cases/*.json')) if not p.endswith('example.json')), None)
    return json.load(open(path)) if path else {}


CASE = load_case()


def _default_host():
    if os.environ.get('ZFS_SALVAGE_HOST'):
        return os.environ['ZFS_SALVAGE_HOST']
    devs = [d.get('path', '').split('@')[0] for d in CASE.get('pool', {}).get('devices', [])]
    return 'local' if devs and all(os.path.exists(d) for d in devs) else 'recovery-host'


HOST = _default_host()
LOCAL = HOST == 'local'
# zsread.py on the recovery host: in the repository when local, else installed by scripts/deploy-tools.sh
READER = os.environ.get('ZFS_SALVAGE_READER') or (f'{REPO}/scripts/zsread.py' if LOCAL else '/usr/local/bin/zsread.py')
ZSCAN = f'{REPO}/tools/zscan/zscan' if LOCAL else 'zscan'


def remote_argv(cmd, host=None, opts=()):
    """argv that runs a shell command on `host` (default: the recovery host): ssh, or bash for `local`."""
    host = host or HOST
    return ['bash', '-c', cmd] if host == 'local' else ['ssh', *opts, host, cmd]


TARGET = CASE.get('target', {})
TARGET_NAME = TARGET.get('dataset', 'target')
_scan = CASE.get('scan', {})
SCAN_DEVICE = _scan.get('device', '')
RAW = f"{REPO}/{_scan.get('chunk_dir') or 'logs/raw/scan_' + (SCAN_DEVICE or 'device')}"
# pools with several top-level vdevs: one scan per vdev (scan-device.sh writes the vdev id into each dir)
RAWS = [f'{REPO}/{d}' for d in _scan.get('chunk_dirs', [])] or [RAW]
SCAN_LOG = f"{REPO}/{_scan.get('log') or 'logs/scan.log'}"


def profile():
    """The pool profile: the case's, overridden by what was measured on a verified tree. The case is
    written before the scan, and zdb may not tell the metadata layout at all (pool does not open)."""
    p = dict(CASE.get('profile', {}))
    try:
        with open(TREE_PROFILE) as f:
            p.update(json.load(f))
    except (OSError, ValueError):
        pass
    return p


def newest_txg():
    """Newest TXG in the case's uberblock timeline (0 if the case has none)."""
    return max((e['txg'] for e in CASE.get('timeline', [])), default=0)


def reader_cmd(*args):
    """Remote command line for zsread.py with the case's devices (per top-level vdev, if the case
    knows them) and label offset."""
    devices = CASE.get('pool', {}).get('devices', [])
    if devices and all(d.get('vdev') is not None for d in devices):
        groups = {}
        for d in devices:
            groups.setdefault(d['vdev'], []).append(d['path'])
        spec = ';'.join(f'{v}={",".join(p)}' for v, p in sorted(groups.items()))
        env = f'ZSREAD_VDEVS={shlex.quote(spec)} '
    else:
        devs = ':'.join(d['path'] for d in devices)
        env = f'ZSREAD_DEVS={shlex.quote(devs)} ' if devs else ''
    if TARGET.get('type') == 'zvol':                         # the .raw file is named after the volume
        env += f"ZSREAD_VOLNAME={shlex.quote(TARGET_NAME.rsplit('/', 1)[-1])} "
    label = CASE.get('profile', {}).get('label_start')
    if label:
        env += f'ZSREAD_LABEL_START={int(label)} '
    return f'{env}python3 -I {READER} ' + ' '.join(args)


SSH_STATE = {'error': None, 'time': 0.0}      # outcome of the most recent ssh() call


def ssh_error():
    """Why the last ssh() call could not reach the recovery host (first line of ssh's message), or None."""
    return SSH_STATE['error']


def ssh(cmd, timeout=20, **kw):
    """Run a command on the recovery host; returns stdout ('' on failure). Whether the host could be
    reached at all is available from ssh_error(): ssh itself fails with exit status 255, any other status
    is the remote command's own."""
    err = None
    try:
        r = subprocess.run(remote_argv(cmd, opts=('-o', 'ConnectTimeout=8', '-o', 'BatchMode=yes')),
                           capture_output=True, text=True, timeout=timeout, **kw)
        if r.returncode == 255:
            err = next((l.strip() for l in r.stderr.splitlines() if l.strip()), 'ssh failed (exit status 255)')
        out = r.stdout
    except subprocess.TimeoutExpired:
        err, out = f'no answer from {HOST} within {timeout} s', ''
    except OSError as e:
        err, out = f'cannot run ssh: {e}', ''
    SSH_STATE['error'], SSH_STATE['time'] = err, time.time()
    return out


def classify(root_names):
    """Which dataset a verified tree belongs to, judged by the names in its root directory.

    The target is recognised by the case's root_names; the rest are generic hints for
    typical Linux datasets, used only for the statistics on the status page.
    """
    s = set(root_names or [])
    if s & set(TARGET.get('root_names', [])):
        return TARGET_NAME
    rules = [('root fs', {'bin', 'etc', 'sbin'}), ('var', {'cache', 'lib', 'log', 'spool'}),
             ('portage', {'profiles', 'metadata', 'eclass'}), ('usr', {'bin', 'lib', 'share', 'include'})]
    for name, keys in rules:
        if len(s & keys) >= 2:
            return name
    if any(re.match(r'linux', n) for n in s):
        return 'usr/src'
    return 'other'
