"""Transfer jobs for the web UI (used by web.py).

Targets:
  local   a path on this machine (Linux path or /mnt/<drive>/... under WSL) -> tar -x -C <path>
  ssh     host:path (ssh config name or user@host; Linux or Windows/OpenSSH) -> ssh host tar -x -f - -C <path>
  rescue  a path on the recovery host itself (e.g. a disk mounted there)

Flow: selected files from the catalog (minus files already transferred OK to this target)
-> ssh <recovery host> 'zsread extract ...' (tar stream on stdout, manifest lines on stderr) -> target.
Every target gets its own directory catalog/transfers/<target-id>/ with manifest.tsv and status.json,
so an interrupted transfer resumes where it stopped.
"""
import glob
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import threading
import time

from common import DB, HOST as RESCUE, REPO, reader_cmd

TDIR = f'{REPO}/catalog/transfers'


def state_args():
    """Candidate (kind, offset, comp, psize, lsize) of the catalogued state, from catalog/state.json."""
    s = json.load(open(f'{REPO}/catalog/state.json'))
    return f"{s['kind']} {s['offset']} {s['comp']} {s['psize']} {s['lsize']}"


def normalize(t):
    """Accept Windows paths for local targets under WSL: 'D:\\rescue' -> '/mnt/d/rescue'."""
    t = dict(t)
    d = t.get('dest', '').strip()
    m = re.match(r'^([A-Za-z]):[\\/]?(.*)$', d)
    if t.get('type') == 'local' and m and os.path.isdir(f'/mnt/{m.group(1).lower()}'):
        d = f'/mnt/{m.group(1).lower()}/' + m.group(2).replace('\\', '/')
    t['dest'] = d.rstrip('/') or '/'
    return t


def label(t):
    """Human-readable target name for the UI."""
    if t['type'] == 'ssh':
        host, path = split_ssh(t['dest'])
        return f'{host} · {path}'
    return ('This computer · ' if t['type'] == 'local' else 'Recovery host · ') + t['dest']


def ssh_hosts():
    """Host aliases from ~/.ssh/config usable as targets (no wildcards, git hosts or the recovery host)."""
    hosts = []
    try:
        for line in open(os.path.expanduser('~/.ssh/config')):
            m = re.match(r'\s*Host\s+(.+)', line, re.I)
            if m:
                hosts += [h for h in m.group(1).split() if not re.search(r'[*?]|^git', h) and h != RESCUE]
    except OSError:
        pass
    return hosts


REAL_FS = ('ext2', 'ext3', 'ext4', 'xfs', 'btrfs', 'zfs', 'ntfs', 'ntfs3', 'fuseblk', 'vfat', 'exfat', 'f2fs')


def _linux_places(host):
    out = subprocess.run(['ssh', '-o', 'ConnectTimeout=8', host, 'findmnt -rnb -o TARGET,AVAIL,FSTYPE,OPTIONS'],
                         capture_output=True, text=True, timeout=20).stdout
    res = []
    for line in out.splitlines():
        c = line.split(' ')
        if len(c) >= 4 and c[2] in REAL_FS and c[1].isdigit() and 'ro' not in c[3].split(',') \
                and not c[0].startswith(('/boot', '/run/archiso')):
            res.append({'path': c[0].replace('\\x20', ' '), 'free': int(c[1]), 'fs': c[2]})
    return res


def places(kind, host=''):
    """Suggested target locations with free space."""
    try:
        if kind == 'local':
            res = []
            for d in sorted(glob.glob('/mnt/?')) + [os.path.expanduser('~')]:
                try:
                    st = os.statvfs(d)
                    res.append({'path': d, 'free': st.f_bavail * st.f_frsize,
                                'fs': f'{d[-1].upper()}: drive' if re.match(r'^/mnt/.$', d) else 'home'})
                except OSError:
                    pass
            return res
        if kind == 'rescue':
            return _linux_places(RESCUE)
        if kind == 'ssh':
            if not host:
                return []
            os_name = subprocess.run(['ssh', '-o', 'ConnectTimeout=8', host, 'echo %OS%'],
                                     capture_output=True, text=True, timeout=20).stdout.strip()
            if 'Windows' not in os_name:
                return _linux_places(host)
            # script via stdin: cmd.exe (the OpenSSH default shell) would split a pipe in the command line
            out = subprocess.run(['ssh', host, 'powershell', '-NoProfile', '-Command', '-'],
                                 input='Get-PSDrive -PSProvider FileSystem | ForEach-Object { $_.Name + "`t" + $_.Free }\n',
                                 capture_output=True, text=True, timeout=30).stdout
            rows = [x.rstrip('\r').split('\t') for x in out.splitlines()]
            return [{'path': f'{r[0]}:/', 'free': int(r[1]), 'fs': f'{r[0]}: drive'}
                    for r in rows if len(r) == 2 and r[1].isdigit() and int(r[1]) > 0]
    except Exception:  # noqa: BLE001
        pass
    return []


def target_id(t):
    return hashlib.sha1(f"{t['type']}|{t['dest']}".encode()).hexdigest()[:10]


def split_ssh(dest):
    if ':' not in dest:
        raise ValueError('ssh target must look like host:path')
    host, path = dest.split(':', 1)
    return host, path or '.'


def is_windows(host):
    return 'Windows' in subprocess.run(['ssh', '-o', 'ConnectTimeout=8', host, 'echo %OS%'],
                                       capture_output=True, text=True, timeout=20).stdout


def dest_cmd(t, windows=False):
    """Command that unpacks the tar stream at the target (creating the folder first)."""
    if t['type'] == 'local':
        os.makedirs(t['dest'], exist_ok=True)
        return ['tar', '-x', '-f', '-', '-C', t['dest']]
    if t['type'] == 'ssh':
        host, path = split_ssh(t['dest'])
        if windows:                       # cmd.exe is the OpenSSH default shell; Windows ships bsdtar
            w = path.replace('/', '\\')
            cmd = f'(if not exist "{w}" mkdir "{w}") & tar -x -f - -C "{path}"'
        else:
            q = shlex.quote(path)
            cmd = f'mkdir -p {q} && tar -x -f - -C {q}'
        return ['ssh', '-o', 'ServerAliveInterval=30', host, cmd]
    raise ValueError('unknown target type')


# Free space is measured at the nearest existing parent: checking never creates anything,
# the folder is only created when the transfer starts.
_LINUX_PROBE = ('p={0}; while [ ! -e "$p" ]; do p=$(dirname "$p"); done; '
                'df -B1 --output=avail "$p" | tail -1; findmnt -no FSTYPE,OPTIONS -T "$p"; '
                '[ -w "$p" ] && echo writable || echo readonly-dir')


def _linux_check(host, path, where):
    if not path.startswith('/'):
        return {'ok': False, 'info': 'use an absolute path (starting with /)'}
    out = subprocess.run(['ssh', '-o', 'ConnectTimeout=8', host, _LINUX_PROBE.format(shlex.quote(path))],
                         capture_output=True, text=True, timeout=30).stdout.split('\n')
    free, fs, w = (out + ['', '', ''])[:3]
    fstype, opts = (fs.split() + ['', ''])[:2]
    if fstype in ('tmpfs', 'overlay'):
        return {'ok': False, 'info': f'{path} is in RAM ({fstype}) – choose a real disk'}
    if 'ro' in opts.split(',') or w.strip() != 'writable':
        return {'ok': False, 'info': f'{path} is read-only'}
    return {'ok': free.strip().isdigit(), 'free': int(free) if free.strip().isdigit() else None,
            'info': f'{where}: {path} ({fstype})'}


def check(t):
    """Reachability, writability and free space of a target (read-only check)."""
    t = normalize(t)
    d = t['dest']
    try:
        if t['type'] == 'local':
            p = d
            while not os.path.exists(p):
                p = os.path.dirname(p)
            if not os.access(p, os.W_OK):
                return {'ok': False, 'info': f'{p} is not writable'}
            st = os.statvfs(p)
            return {'ok': True, 'free': st.f_bavail * st.f_frsize, 'dest': d,
                    'info': f'this computer: {d}' + ('' if p == d else ' (will be created)')}
        if t['type'] == 'ssh':
            host, path = split_ssh(d)
            if not is_windows(host):
                return _linux_check(host, path, host)
            if not re.match(r'^[A-Za-z]:[\\/]', path):
                return {'ok': False, 'info': 'use a Windows path like F:/rescued-home'}
            out = subprocess.run(['ssh', host, 'powershell', '-NoProfile', '-Command', '-'],
                                 input=f'(Get-PSDrive {path[0]}).Free\n', capture_output=True, text=True,
                                 timeout=30).stdout.strip()
            return {'ok': out.isdigit(), 'free': int(out) if out.isdigit() else None,
                    'info': f'{host} (Windows): {path}' if out.isdigit() else f'drive {path[0]}: not found on {host}'}
        if t['type'] == 'rescue':
            return _linux_check(RESCUE, d, 'recovery host')
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'info': f'{type(e).__name__}: {e}'}
    return {'ok': False, 'info': 'unknown target type'}


class Job:
    def __init__(self, target):
        self.t = target
        self.id = target_id(target)
        self.dir = f'{TDIR}/{self.id}'
        os.makedirs(self.dir, exist_ok=True)
        json.dump(target, open(f'{self.dir}/target.json', 'w'), ensure_ascii=False)
        self.manifest = f'{self.dir}/manifest.tsv'
        self.status = {'id': self.id, 'target': target, 'label': label(target), 'state': 'prepared', 'files_total': 0, 'bytes_total': 0,
                       'files': 0, 'bytes': 0, 'ok': 0, 'partial': 0, 'errors': 0, 'current': '',
                       'start': None, 'end': None, 'recent': []}
        self.proc = []
        self.stop_flag = False

    def done_paths(self):
        done = set()
        if os.path.exists(self.manifest):
            for line in open(self.manifest, encoding='utf-8', errors='surrogateescape'):
                c = line.rstrip('\n').split('\t')
                if len(c) > 2 and c[1] in ('OK', 'LINK'):
                    done.add(c[2])
        return done

    def todo(self):
        db = sqlite3.connect(DB)
        done = self.done_paths()
        rows = [(o, p, s or 0) for o, p, s in db.execute(
            "select obj, path, size from entry where sel=1 and kind in ('f','l') and status='ok' order by path")
            if p not in done]
        return rows, len(done)

    def write_status(self):
        json.dump(self.status, open(f'{self.dir}/status.json.tmp', 'w'), ensure_ascii=False)
        os.replace(f'{self.dir}/status.json.tmp', f'{self.dir}/status.json')

    def run(self):
        rows, already = self.todo()
        st = self.status
        st.update(state='running', files_total=len(rows), bytes_total=sum(r[2] for r in rows),
                  already_done=already, start=time.time())
        self.write_status()
        if not rows:
            st.update(state='done', end=time.time())
            return self.write_status()
        src = ['ssh', '-o', 'ServerAliveInterval=30', RESCUE, reader_cmd('extract', state_args())]
        if self.t['type'] == 'rescue':
            q = shlex.quote(self.t['dest'])
            src[-1] += f' | (mkdir -p {q} && tar -x -f - -C {q})'
            p1 = subprocess.Popen(src, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            self.proc = [p1]
        else:
            p1 = subprocess.Popen(src, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            win = self.t['type'] == 'ssh' and is_windows(split_ssh(self.t['dest'])[0])
            p2 = subprocess.Popen(dest_cmd(self.t, win), stdin=p1.stdout, stderr=subprocess.DEVNULL)
            p1.stdout.close()
            self.proc = [p1, p2]

        def feed():
            try:
                for o, p, _ in rows:
                    if self.stop_flag:
                        break
                    p1.stdin.write(f'{o}\t{p}\n'.encode('utf-8', 'surrogateescape'))
                p1.stdin.close()
            except BrokenPipeError:
                pass
        threading.Thread(target=feed, daemon=True).start()

        with open(self.manifest, 'a', encoding='utf-8', errors='surrogateescape') as man:
            for raw in p1.stderr:
                line = raw.decode('utf-8', 'surrogateescape').rstrip('\n')
                if not line.startswith('M\t'):
                    continue
                man.write(line + '\n')
                man.flush()
                c = line.split('\t')
                kind, path, size = c[1], c[2], int(c[3]) if c[3].isdigit() else 0
                st['files'] += 1
                st['bytes'] += size
                st['current'] = path
                if kind in ('OK', 'LINK'):
                    st['ok'] += 1
                elif kind == 'PARTIAL':
                    st['partial'] += 1
                else:
                    st['errors'] += 1
                st['recent'] = ([f'{kind}  {path}'] + st['recent'])[:12]
                if st['files'] % 5 == 0:
                    self.write_status()
        for p in self.proc:
            p.wait()
        rc = [p.returncode for p in self.proc]
        st.update(state='stopped' if self.stop_flag else ('done' if all(r == 0 for r in rc) else f'failed (exit {rc})'),
                  end=time.time(), current='')
        self.write_status()

    def stop(self):
        self.stop_flag = True
        for p in self.proc:
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                pass


JOBS = {}


def start(target):
    j = Job(normalize(target))
    if j.id in JOBS and JOBS[j.id].status['state'] == 'running':
        return JOBS[j.id].status
    JOBS[j.id] = j
    threading.Thread(target=j.run, daemon=True).start()
    time.sleep(0.5)
    return j.status


def status():
    out = []
    if os.path.isdir(TDIR):
        for d in sorted(os.listdir(TDIR)):
            f = f'{TDIR}/{d}/status.json'
            if d in JOBS:
                out.append(JOBS[d].status)
            elif os.path.exists(f):
                st = json.load(open(f))
                st.setdefault('label', label(st['target']))
                out.append(st)
    return out
