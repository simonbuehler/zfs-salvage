"""Step 0 of the web UI, "Set up": checks, pool devices, examining the pool, the case file, scan and rank.

Everything that takes long runs as a job (jobs.py). The tool never asks for passwords and never unlocks, partitions or
assembles anything: for that the page shows the command to run. Making a device read-only is the one change it makes
on its own, and only on request: a kernel flag for block devices (`blockdev --setro`, nothing is written to the disk),
the write bits for image files.
"""
import json
import os
import re
import stat
import subprocess
import sys

import common
import doctor
import jobs

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE = f'{common.STATE}/setup/probe.json'
ZDB = os.environ.get('ZFS_SALVAGE_ZDB') or 'zdb'
RESTART = {'pending': False}          # set when a new case was written: web.py restarts itself to load it


def run_doctor():
    argv = [sys.executable, '-I', f'{HERE}/doctor.py', '--json'] + ([] if os.geteuid() == 0 else ['--no-root'])
    r = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    try:
        return json.loads(r.stdout)
    except ValueError:
        return [{'check': 'doctor', 'status': 'FAIL', 'text': (r.stderr or r.stdout).strip()[-300:], 'hint': ''}]


def devices(extra=()):
    """ZFS member devices the system sees (or the case's), plus paths the user typed, with their read-only state."""
    found, _ = doctor.member_devices()
    try:                                                # what was examined last (survives a reload of the page)
        with open(PROBE) as f:
            found = list(found) + json.load(f).get('devices', [])
    except (OSError, ValueError):
        pass
    out = []
    for d in dict.fromkeys(list(found) + [x for x in extra if x]):
        out.append({'path': d, 'exists': os.path.exists(d), 'image': os.path.isfile(d),
                    'read_only': os.path.exists(d) and doctor.read_only(d)})
    return out


def make_read_only(dev):
    if not os.path.exists(dev):
        return {'ok': False, 'info': f'{dev} does not exist'}
    if os.path.isfile(dev):
        os.chmod(dev, os.stat(dev).st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    elif stat.S_ISBLK(os.stat(dev).st_mode):
        r = subprocess.run(['blockdev', '--setro', dev], capture_output=True, text=True)
        if r.returncode:
            return {'ok': False, 'info': (r.stderr or r.stdout).strip() or f'blockdev --setro failed ({r.returncode})'}
    else:
        return {'ok': False, 'info': f'{dev} is neither a block device nor an image file'}
    return {'ok': doctor.read_only(dev), 'info': f'{dev} is read-only now' if doctor.read_only(dev) else 'still writable'}


def _check_devs(devs):
    devs = [d for d in devs or [] if isinstance(d, str) and d]
    if not devs:
        return None, 'choose at least one device'
    writable = [d for d in devs if not os.path.exists(d) or not doctor.read_only(d)]
    if writable:
        return None, f'not read-only (or missing): {", ".join(writable)} – make them read-only first'
    return devs, None


def probe(body):
    devs, err = _check_devs(body.get('devs'))
    if err:
        return {'state': 'refused', 'info': err}
    if os.path.exists(PROBE):
        os.remove(PROBE)
    os.makedirs(os.path.dirname(PROBE), exist_ok=True)
    argv = [sys.executable, '-I', f'{HERE}/make-case.py', '--zdb', ZDB, '--probe-json', PROBE] + sum([['--dev', d] for d in devs], [])
    return PROBE_JOB.start(argv, jobs.log_path('make-case'))


def _case_written(job):
    """After make-case: remember the new case and let web.py restart with it."""
    if job.st.get('exit') != 0:
        return
    written = [m.group(1) for line in jobs.tail(job.st['log'], 40) for m in [re.match(r'== written (\S+\.json)$', line)] if m]
    if written:
        with open(common.ACTIVE_CASE, 'w') as f:
            f.write(written[-1])
        RESTART['pending'] = True


def create_case(body):
    devs, err = _check_devs(body.get('devs'))
    if err:
        return {'state': 'refused', 'info': err}
    target = body.get('target')
    if not isinstance(target, str) or not target:
        return {'state': 'refused', 'info': 'choose the dataset to recover'}
    if SCAN_JOB.running() or RANK_JOB.running():
        return {'state': 'refused', 'info': 'pause the scan first'}
    names = ','.join(n.strip() for n in (body.get('root_names') or '').split(',') if n.strip())
    argv = ([sys.executable, '-I', f'{HERE}/make-case.py', '--zdb', ZDB, '--yes', '--target', target, '--root-names', names,
             '--out', common.CASES] + sum([['--dev', d] for d in devs], []))
    return CASE_JOB.start(argv, jobs.log_path('make-case'))


def scan_devices():
    """(device, chunk dir, log) per top-level vdev: one scan for each, the first one logs where the status page reads."""
    devs = common.CASE.get('pool', {}).get('devices', [])
    by_vdev = {}
    for d in devs:
        by_vdev.setdefault(d.get('vdev') or 0, d['path'].split('@')[0])
    first = [by_vdev[v] for v in sorted(by_vdev)]
    out = []
    for i, (dev, raw) in enumerate(zip(first, common.RAWS)):
        out.append((dev, raw, common.SCAN_LOG if i == 0 else f'{common.LOGS}/scan_{os.path.basename(dev)}.log'))
    return out


def failing_disks():
    bad = []
    for dev, _, _ in scan_devices():
        for disk in doctor.disks_of(dev):
            problems, _ = doctor.smart_problems(disk)
            if problems:
                bad.append(f'{disk}: {", ".join(problems)}')
    return bad


def _scan_done(job):
    if job.st.get('state') == 'done':                 # the last chunks: rank once more
        start_rank()


def start_rank():
    return RANK_JOB.start([sys.executable, '-I', f'{HERE}/rank-trees.py', '--loop'], f'{common.LOGS}/rank-trees.log')


def start_scan(body):
    if not common.CASE:
        return {'state': 'refused', 'info': 'create the case first'}
    bad = failing_disks()
    if bad and not body.get('anyway'):
        return {'state': 'needs_confirmation', 'info': 'This disk is failing – ' + '; '.join(bad) +
                '. Every pass over it costs life; an image made with ddrescue first is safer (README, "Failing disks").'}
    steps = [f'bash {HERE}/scan-device.sh {sh(dev)} 64 {sh(raw)} >> {sh(log)} 2>&1' for dev, raw, log in scan_devices()]
    os.makedirs(common.LOGS, exist_ok=True)
    st = SCAN_JOB.start(['bash', '-c', ' && '.join(steps)], f'{common.LOGS}/scan-job.log')
    start_rank()
    return st


def pause_scan():
    RANK_JOB.stop()
    return SCAN_JOB.stop()


def sh(x):
    import shlex
    return shlex.quote(x)


def state():
    try:
        with open(PROBE) as f:
            found = json.load(f)
    except (OSError, ValueError):
        found = None
    case = None
    if common.CASE:
        case = {k: common.CASE.get(k) for k in ('name', 'diagnosis', 'compatibility', 'unsupported', 'problems')}
        case['pool'] = common.CASE.get('pool', {}).get('name')
        case['target'] = common.CASE.get('target', {}).get('dataset')
        case['devices'] = [d['path'] for d in common.CASE.get('pool', {}).get('devices', [])]
    js = {j.name: j.status() for j in (PROBE_JOB, CASE_JOB, SCAN_JOB, RANK_JOB)}
    for n in js:
        js[n]['tail'] = jobs.tail(js[n]['log']) if js[n].get('log') else []
    if common.CASE:
        js['scan']['tail'] = jobs.tail(common.SCAN_LOG)          # the scan's own progress lines
    return {'probe': found, 'case': case, 'jobs': js, 'restarting': RESTART['pending']}


def resume():
    """At the start of web.py."""
    for j in (SCAN_JOB, RANK_JOB, PROBE_JOB, CASE_JOB):
        j.resume()


def cancel(name):
    return {'probe': PROBE_JOB, 'case': CASE_JOB}[name].stop()


PROBE_JOB = jobs.job('probe')
CASE_JOB = jobs.job('case', on_exit=_case_written)
SCAN_JOB = jobs.job('scan', restart=True, on_exit=_scan_done)
RANK_JOB = jobs.job('rank', restart=True)
