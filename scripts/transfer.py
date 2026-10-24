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
import base64
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

import doctor
from common import CATALOG, DB, HOST as RESCUE, reader_cmd, remote_argv

TDIR = f'{CATALOG}/transfers'


def state_args():
    """Candidate (kind, offset, comp, psize, lsize) of the catalogued state, from catalog/state.json."""
    s = json.load(open(f'{CATALOG}/state.json'))
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
    out = subprocess.run(remote_argv('findmnt -rnb -o TARGET,AVAIL,FSTYPE,OPTIONS', host, ('-o', 'ConnectTimeout=8')),
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


def _pool_probe(devs):
    """Shell lines that print the disk below the target's file system (TDISK) and below each pool device (PDISK)."""
    return ('; dk() { lsblk -psnro NAME,TYPE "$1" 2>/dev/null | awk \'$2=="disk"{printf "%s ", $1}\'; }; '
            'echo "TDISK $(dk "$(findmnt -no SOURCE -T "$p")")"; '
            + ''.join(f'echo "PDISK {shlex.quote(d)} $(dk {shlex.quote(d)})"; ' for d in devs))


def _linux_check(host, path, where, pool_devs=()):
    if not path.startswith('/'):
        return {'ok': False, 'info': 'use an absolute path (starting with /)'}
    probe = _LINUX_PROBE.format(shlex.quote(path)) + (_pool_probe(pool_devs) if pool_devs else '')
    out = subprocess.run(remote_argv(probe, host, ('-o', 'ConnectTimeout=8')),
                         capture_output=True, text=True, timeout=30).stdout.split('\n')
    free, fs, w = (out + ['', '', ''])[:3]
    fstype, opts = (fs.split() + ['', ''])[:2]
    if fstype in ('tmpfs', 'overlay'):
        return unsafe(f'{path} is in RAM ({fstype}) – choose a real disk')
    if 'ro' in opts.split(',') or w.strip() != 'writable':
        return unsafe(f'{path} is read-only')
    tdisks = {x for l in out if l.startswith('TDISK ') for x in l.split()[1:]}
    for l in out:
        parts = l.split()
        shared = tdisks & set(parts[2:]) if parts[:1] == ['PDISK'] else set()
        if shared:
            return unsafe(f'{path}: {sorted(shared)[0]} also holds the pool ({parts[1]}): writing there could overwrite '
                          'the very data you want to rescue – choose another disk')
    return {'ok': free.strip().isdigit(), 'free': int(free) if free.strip().isdigit() else None,
            'info': f'{where}: {path} ({fstype})'}


def unsafe(info):
    """A target the tool refuses on principle (RAM, read-only, the pool's own disk): `unsafe` makes start() refuse too."""
    return {'ok': False, 'unsafe': True, 'info': info}


def pool_disk_conflict(target_source):
    """Message if the disk below the target's file system also holds a pool member, else None. Pool members that are
    image files have no disk of their own."""
    mine = set(doctor.disks_of(target_source)) if target_source else set()
    if not mine:
        return None
    for dev in doctor.case_devices():
        shared = mine & set(doctor.disks_of(dev))
        if shared:
            return (f'{sorted(shared)[0]} also holds the pool ({dev}): writing there could overwrite the very data you '
                    'want to rescue – choose another disk')
    return None


def disk_warning(target_source):
    """Warning if SMART says the target's disk is failing (rescued files would be at risk), else None."""
    bad = []
    for d in sorted(set(doctor.disks_of(target_source)) if target_source else ()):
        problems, _ = doctor.smart_problems(d)
        if problems:
            bad.append(f'{d}: {", ".join(problems)}')
    return ('this disk is failing – ' + '; '.join(bad) + ' – the rescued files may not survive there') if bad else None


def check_local_target(path):
    """Safety of a local target directory, judged by the mount that holds it (nearest existing parent `path`)."""
    mp, fstype, source, opts = doctor.mount_info(path)
    if fstype in doctor.RAM_DISK_FS and not os.environ.get('ZFS_SALVAGE_ALLOW_RAM_TARGET'):
        return unsafe(f'{path} is in RAM ({fstype}) – the files would be gone after a reboot; choose a real disk')
    if 'ro' in opts.split(','):
        return unsafe(f'{path} is on a read-only file system')
    conflict = pool_disk_conflict(source)
    if conflict:
        return unsafe(f'{path}: {conflict}')
    warning = disk_warning(source)
    return {'warning': warning} if warning else {}


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
            verdict = check_local_target(p)
            if verdict.get('unsafe'):
                return verdict
            st = os.statvfs(p)
            return {'ok': True, 'free': st.f_bavail * st.f_frsize, 'dest': d,
                    'info': f'this computer: {d}' + ('' if p == d else ' (will be created)'), **verdict}
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
            return _linux_check(RESCUE, d, 'recovery host', doctor.case_devices())
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

    def manifest_lines(self):
        if os.path.exists(self.manifest):
            with open(self.manifest, encoding='utf-8', errors='surrogateescape') as f:
                for line in f:
                    yield line.rstrip('\n').split('\t')

    def done_paths(self):
        """Files that count as copied. A manifest line is written when the *source* has read a file, so the
        last files of a run that did not end cleanly (stopped, killed, connection lost) may be cut off on the
        target: they do not count and are copied again. A run is clean if its END marker is there.
        A manifest without run markers (older versions) counts as clean if the job ended with `done`."""
        runs, cur = [], None
        for c in self.manifest_lines():
            if c[0] == 'R' and len(c) > 1:
                if c[1] == 'START':
                    cur = [False, []]
                    runs.append(cur)
                elif c[1] == 'END' and cur:
                    cur[0] = True
            elif c[0] == 'M' and len(c) > 2 and c[1] in ('OK', 'LINK'):
                if cur is None:                                   # no markers at all: an older manifest
                    cur = [self.previous_state() == 'done', []]
                    runs.append(cur)
                cur[1].append(c[2])
        done = set()
        for clean, paths in runs:
            done.update(paths if clean else paths[:max(0, len(paths) - UNCLEAN_TAIL)])
        return done

    def previous_state(self):
        try:
            with open(f'{self.dir}/status.json') as f:
                return json.load(f).get('state')
        except (OSError, ValueError):
            return None

    def todo(self, only=None):
        db = sqlite3.connect(DB)
        done = set() if only is not None else self.done_paths()
        rows = [(o, p, s or 0) for o, p, s in db.execute(
            "select obj, path, size from entry where sel=1 and kind in ('f','l') and status='ok' order by path")
            if p not in done and (only is None or p in only)]
        return rows, len(done)

    def write_status(self):
        json.dump(self.status, open(f'{self.dir}/status.json.tmp', 'w'), ensure_ascii=False)
        os.replace(f'{self.dir}/status.json.tmp', f'{self.dir}/status.json')

    def run(self, only=None):
        rows, already = self.todo(only)
        st = self.status
        st.update(state='running', files_total=len(rows), bytes_total=sum(r[2] for r in rows),
                  already_done=already, start=time.time())
        self.write_status()
        if not rows:
            st.update(state='done', end=time.time())
            return self.write_status()
        src = remote_argv(reader_cmd('extract', state_args()), opts=('-o', 'ServerAliveInterval=30'))
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
            man.write(f'R\tSTART\t{int(time.time())}\n')
            man.flush()
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
        clean = not self.stop_flag and all(r == 0 for r in rc)
        if clean:                                  # only now do the files of this run count as copied
            with open(self.manifest, 'a', encoding='utf-8') as man:
                man.write(f'R\tEND\t{int(time.time())}\n')
        st.update(state='stopped' if self.stop_flag else ('done' if clean else f'failed (exit {rc})'),
                  end=time.time(), current='')
        self.write_status()
        if clean and AUTO_VERIFY:
            self.verify(only)

    def stop(self):
        self.stop_flag = True
        for p in self.proc:
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                pass


    # ------------------------------------------------------------------ verify the copy on the target
    def expected(self):
        """{path: (size, sha256)} of every file the manifest reports as read OK (the last report wins)."""
        exp = {}
        for c in self.manifest_lines():
            if c[0] == 'M' and len(c) > 5 and c[1] == 'OK' and len(c[4]) == 64:
                exp[c[2]] = (int(c[3]) if c[3].isdigit() else None, c[4])
        return exp

    def verify_file(self):
        return f'{self.dir}/verify.json'

    def verify_state(self):
        try:
            with open(self.verify_file()) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def verify(self, only=None):
        """Hash the copied files on the target and compare with the manifest. only: a set of paths (after a
        re-copy); the result of the other files is kept. Result in verify.json and status['verify']."""
        exp = self.expected()
        paths = sorted(exp if only is None else [p for p in exp if p in only])
        old = self.verify_state() or {}
        keep_bad = {} if only is None else {p: r for p, r in old.get('bad', {}).items() if p not in only}
        v = {'state': 'running', 'total': len(exp), 'checked': 0, 'start': time.time(), 'bad': dict(keep_bad)}
        self.status['verify'] = v
        self.write_status()
        for i in range(0, len(paths), VERIFY_CHUNK):
            chunk = paths[i:i + VERIFY_CHUNK]
            try:
                got = hash_on_target(self.t, chunk)
            except Exception as e:  # noqa: BLE001 - report it, do not claim the copy is fine
                v.update(state='failed', error=f'{type(e).__name__}: {e}')
                self.write_status()
                return v
            for p in chunk:
                h = got.get(p)
                if h is None:
                    v['bad'][p] = 'missing'
                elif h != exp[p][1]:
                    v['bad'][p] = 'differs'
            v['checked'] += len(chunk)
            self.write_status()
        v.update(state='done', end=time.time(), ok=len(exp) - len(v['bad']))
        with open(self.verify_file(), 'w') as f:
            json.dump(v, f, ensure_ascii=False)
        self.write_status()
        return v

    def recopy(self):
        """Copy again the files the last verification found missing or different, then verify just those."""
        bad = set((self.verify_state() or {}).get('bad', {}))
        if not bad:
            return None
        self.run(only=bad)                     # verifies the same files at the end of a clean run
        return self.status.get('verify')


UNCLEAN_TAIL = 50          # files of a run that did not end cleanly that are copied again
VERIFY_CHUNK = 100         # files per call to the target
AUTO_VERIFY = os.environ.get('ZFS_SALVAGE_AUTO_VERIFY', '1') != '0'


def _b64(path):
    return base64.b64encode(path.lstrip('/').encode('utf-8', 'surrogateescape')).decode()


def hash_on_target(t, paths):
    """{path: sha256 hex, or None if the file is not there} computed on the target itself, never by
    reading the files back through the network. paths are relative to the target folder, with a leading '/'."""
    root = t['dest']
    if t['type'] == 'local':
        out = {}
        for p in paths:
            f = os.path.join(root, p.lstrip('/'))
            if not os.path.isfile(f):
                out[p] = None
                continue
            h = hashlib.sha256()
            with open(f, 'rb') as fh:
                for block in iter(lambda: fh.read(1 << 20), b''):
                    h.update(block)
            out[p] = h.hexdigest()
        return out
    host, path = (split_ssh(root) if t['type'] == 'ssh' else (RESCUE, root))
    if t['type'] == 'ssh' and is_windows(host):
        script = ("$r='" + path.replace("'", "''").replace('/', '\\') + "'; $items=@('" + "','".join(_b64(p) for p in paths) + "'); "
                  "foreach($b in $items){ $rel=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b)).Replace('/','\\'); "
                  "$f=Join-Path $r $rel; if(Test-Path -LiteralPath $f -PathType Leaf){ "
                  "$h=(Get-FileHash -Algorithm SHA256 -LiteralPath $f).Hash.ToLower() } else { $h='MISSING' }; "
                  "Write-Output ($b+' '+$h) }\n")
        r = subprocess.run(['ssh', '-o', 'ServerAliveInterval=30', host, 'powershell', '-NoProfile', '-Command', '-'],
                           input=script, capture_output=True, text=True, timeout=7200)
        res = dict(l.split() for l in r.stdout.splitlines() if len(l.split()) == 2)
        if len(res) != len(paths):
            raise RuntimeError(f'target answered for {len(res)} of {len(paths)} files: {r.stderr.strip()[-200:]}')
        return {p: (None if res[_b64(p)] == 'MISSING' else res[_b64(p)]) for p in paths}
    # Linux target (also the recovery host): sha256sum on the target, names NUL-separated through stdin
    q = shlex.quote(path)
    r = subprocess.run(remote_argv(f'cd {q} && xargs -0 -r sha256sum 2>/dev/null', host, ('-o', 'ServerAliveInterval=30')),
                       input='\0'.join(p.lstrip('/') for p in paths).encode('utf-8', 'surrogateescape'),
                       capture_output=True, timeout=7200)
    res = {}
    for line in r.stdout.decode('utf-8', 'surrogateescape').splitlines():
        h, _, name = line.partition('  ')
        if len(h) == 64:
            res['/' + name] = h
    return {p: res.get(p) for p in paths}


JOBS = {}


def start(target):
    verdict = check(target)
    if verdict.get('unsafe'):                 # the UI checks first, but the server does not rely on that
        return {'state': 'refused', 'info': verdict['info']}
    j = Job(normalize(target))
    if j.id in JOBS and JOBS[j.id].status['state'] == 'running':
        return JOBS[j.id].status
    JOBS[j.id] = j
    threading.Thread(target=j.run, daemon=True).start()
    time.sleep(0.5)
    return j.status


def job_for(job_id):
    """The Job object of a transfer target (from memory, or rebuilt from its folder)."""
    if job_id in JOBS:
        return JOBS[job_id]
    folder = f'{TDIR}/{os.path.basename(job_id)}'
    with open(f'{folder}/target.json') as f:
        j = Job(json.load(f))
    try:                                      # keep what the earlier run reported (state, counts, times)
        with open(f'{folder}/status.json') as f:
            j.status.update(json.load(f))
    except (OSError, ValueError):
        pass
    if (j.status.get('verify') or {}).get('state') == 'running':
        j.status['verify']['state'] = 'interrupted'
    JOBS[job_id] = j
    return j


def _busy(j):
    return j.status['state'] == 'running' or (j.status.get('verify') or {}).get('state') == 'running'


def start_verify(job_id):
    j = job_for(job_id)
    if not _busy(j):
        threading.Thread(target=j.verify, daemon=True).start()
        time.sleep(0.3)
    return public(j.status)


def start_recopy(job_id):
    j = job_for(job_id)
    if not _busy(j):
        threading.Thread(target=j.recopy, daemon=True).start()
        time.sleep(0.3)
    return public(j.status)


def public(st):
    """Status for the UI: the verification result as counts and a short list of the affected files."""
    st = dict(st)
    v = st.get('verify')
    if v:
        bad = v.get('bad', {})
        st['verify'] = {**{k: x for k, x in v.items() if k != 'bad'}, 'bad_n': len(bad),
                        'missing_n': sum(1 for r in bad.values() if r == 'missing'),
                        'bad_sample': [f'{r}: {p}' for p, r in list(bad.items())[:8]]}
    return st


def status():
    out = []
    if os.path.isdir(TDIR):
        for d in sorted(os.listdir(TDIR)):
            f = f'{TDIR}/{d}/status.json'
            if d in JOBS:
                st = JOBS[d].status
            elif os.path.exists(f):
                with open(f) as fh:
                    st = json.load(fh)
                st.setdefault('label', label(st['target']))
                if st.get('verify', {}).get('state') == 'running':        # the server was restarted meanwhile
                    st['verify']['state'] = 'interrupted'
            else:
                continue
            out.append(public(st))
    return out
