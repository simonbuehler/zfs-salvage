"""Shared settings for the workstation-side scripts.

Everything pool-specific comes from the case file (cases/<name>.json, written by make-case.py;
pick one with ZFS_SALVAGE_CASE=<path> if there are several). The recovery host is an SSH alias
or user@host in ZFS_SALVAGE_HOST (default: recovery-host).
"""
import glob
import json
import os
import re
import shlex
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST = os.environ.get('ZFS_SALVAGE_HOST', 'recovery-host')
READER = '/usr/local/bin/zsread.py'          # installed by scripts/deploy-tools.sh

STATUS = f'{REPO}/status'
CANDIDATES = f'{STATUS}/target_candidates.json'
PROBED = f'{STATUS}/probed.jsonl'
PROBE_STATS = f'{STATUS}/probe_stats.json'
WORKFLOW = f'{STATUS}/workflow.json'
DB = f'{REPO}/catalog/catalog.sqlite'


def load_case():
    path = os.environ.get('ZFS_SALVAGE_CASE') or next(
        (p for p in sorted(glob.glob(f'{REPO}/cases/*.json')) if not p.endswith('example.json')), None)
    return json.load(open(path)) if path else {}


CASE = load_case()
TARGET = CASE.get('target', {})
TARGET_NAME = TARGET.get('dataset', 'target')
_scan = CASE.get('scan', {})
SCAN_DEVICE = _scan.get('device', '')
RAW = f"{REPO}/{_scan.get('chunk_dir') or 'logs/raw/scan_' + (SCAN_DEVICE or 'device')}"
# pools with several top-level vdevs: one scan per vdev (scan-device.sh writes the vdev id into each dir)
RAWS = [f'{REPO}/{d}' for d in _scan.get('chunk_dirs', [])] or [RAW]
SCAN_LOG = f"{REPO}/{_scan.get('log') or 'logs/scan.log'}"


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
    label = CASE.get('profile', {}).get('label_start')
    if label:
        env += f'ZSREAD_LABEL_START={int(label)} '
    return f'{env}python3 -I {READER} ' + ' '.join(args)


def ssh(cmd, timeout=20, **kw):
    """Run a command on the recovery host; returns stdout ('' on failure)."""
    try:
        return subprocess.run(['ssh', '-o', 'ConnectTimeout=8', '-o', 'BatchMode=yes', HOST, cmd],
                              capture_output=True, text=True, timeout=timeout, **kw).stdout
    except (subprocess.TimeoutExpired, OSError):
        return ''


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
