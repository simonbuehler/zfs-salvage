"""Shared settings for the workstation-side scripts.

Everything pool-specific comes from the case file (cases/<name>.json, written by make-case.py;
pick one with ZFS_SALVAGE_CASE=<path> if there are several). Everything runs on the machine that has the
pool's devices; to use the web UI from another computer, forward its port with `ssh -L`.
"""
import glob
import json
import os
import shlex
import subprocess

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
TREE_GROUPS = f'{STATUS}/tree_groups.json'       # the different file systems found among the verified trees
CATALOG = os.environ.get('ZFS_SALVAGE_CATALOG_DIR') or f'{REPO}/catalog'     # catalog, selection, transfers
DB = f'{CATALOG}/catalog.sqlite'
CASES = os.environ.get('ZFS_SALVAGE_CASES_DIR') or f'{REPO}/cases'          # case files (make-case.py writes here)


def _under_state(p):
    """Paths in a case file are relative to the state directory (the repository by default) unless absolute."""
    return p if os.path.isabs(p) else f'{STATE}/{p}'


ACTIVE_CASE = f'{STATE}/active-case'             # path of the case made in the setup page (several may exist)


def case_path():
    if os.environ.get('ZFS_SALVAGE_CASE'):
        return os.environ['ZFS_SALVAGE_CASE']
    try:
        with open(ACTIVE_CASE) as f:
            active = f.read().strip()
        if os.path.exists(active):
            return active
    except OSError:
        pass
    return next((p for p in sorted(glob.glob(f'{CASES}/*.json')) if not p.endswith('example.json')), None)


def load_case():
    path = case_path()
    if not path:
        return {}
    with open(path) as f:
        return json.load(f)


CASE = load_case()


READER = os.environ.get('ZFS_SALVAGE_READER') or f'{REPO}/scripts/zsread.py'
ZSCAN = f'{REPO}/tools/zscan/zscan'


def remote_argv(cmd, host='local', opts=()):
    """argv that runs a shell command here (`local`) or on a transfer target over ssh."""
    return ['bash', '-c', cmd] if host == 'local' else ['ssh', *opts, host, cmd]


TARGET = CASE.get('target', {})
TARGET_NAME = TARGET.get('dataset', 'target')


def reload_case():
    """Read the case file again, in place: modules that imported CASE or TARGET see the change (a long-running
    rank-trees loop and the web server pick up a target chosen in the UI this way)."""
    new = load_case()
    CASE.clear()
    CASE.update(new)
    TARGET.clear()
    TARGET.update(CASE.get('target', {}))
    if 'target' in CASE:
        CASE['target'] = TARGET


def set_root_names(names):
    """Remember in the case file which top-level names identify the target file system."""
    path = case_path()
    if not path:
        raise ValueError('no case file yet')
    with open(path) as f:
        case = json.load(f)
    case.setdefault('target', {})['root_names'] = list(names)
    with open(path + '.tmp', 'w') as f:
        json.dump(case, f, indent=1, ensure_ascii=False)
    os.replace(path + '.tmp', path)
    reload_case()
_scan = CASE.get('scan', {})
SCAN_DEVICE = _scan.get('device', '')
RAW = _under_state(_scan.get('chunk_dir') or 'logs/raw/scan_' + (SCAN_DEVICE or 'device'))
# pools with several top-level vdevs: one scan per vdev (scan-device.sh writes the vdev id into each dir)
RAWS = [_under_state(d) for d in _scan.get('chunk_dirs', [])] or [RAW]
SCAN_LOG = _under_state(_scan.get('log') or 'logs/scan.log')


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
    """Command line for zsread.py with the case's devices (per top-level vdev, if the case
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


def shell(cmd, timeout=20):
    """Run a shell command here; returns stdout ('' on failure or timeout)."""
    try:
        return subprocess.run(['bash', '-c', cmd], capture_output=True, text=True, timeout=timeout).stdout
    except (subprocess.TimeoutExpired, OSError):
        return ''


def classify(root_names):
    """Which dataset a verified tree belongs to: the target if its root directory has one of the case's root_names
    (optional: without them nothing is the target and the user picks one of the found groups, see
    rank-trees.tree_groups), else 'other'."""
    if set(root_names or []) & set(TARGET.get('root_names', [])):
        return TARGET_NAME
    return 'other'
