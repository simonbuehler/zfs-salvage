"""Long-running jobs of the web UI: examining the pool, writing the case, scanning and ranking, run as child
processes of web.py.

Every job has a state file <state>/jobs/<name>.json: what the user wants (`want`: run or stop), the pid while it runs, the
last exit code and the number of restarts. A job runs at most once: a second start returns the running one, and a live pid
in the state file (another web.py) blocks it too. Jobs marked `restart` (scan, rank) are started again after a crash, up to
three times, and when web.py starts and the user had not stopped them; the others are reported as interrupted. When web.py
ends, its jobs are stopped (`want` is kept, so they resume next time).
"""
import json
import os
import signal
import subprocess
import threading
import time

from common import LOGS, STATE

DIR = f'{STATE}/jobs'
MAX_RESTARTS = 3
_lock = threading.Lock()
JOBS = {}


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False


class Job:
    def __init__(self, name, restart=False, on_exit=None):
        self.name, self.restart, self.on_exit = name, restart, on_exit
        self.proc = None
        self.path = f'{DIR}/{name}.json'
        self.st = {'name': name, 'state': 'idle', 'want': 'stop', 'pid': None, 'argv': None, 'log': None,
                   'started': None, 'ended': None, 'exit': None, 'restarts': 0, 'info': ''}
        try:
            with open(self.path) as f:
                self.st.update(json.load(f))
        except (OSError, ValueError):
            pass

    def save(self):
        os.makedirs(DIR, exist_ok=True)
        with open(self.path + '.tmp', 'w') as f:
            json.dump(self.st, f)
        os.replace(self.path + '.tmp', self.path)

    def status(self):
        return dict(self.st)

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, argv, log, env=None, info=''):
        with _lock:
            if self.running():
                return self.status()
            if self.st.get('state') == 'running' and _alive(self.st.get('pid')):
                return dict(self.st, info='already running in another process')
            self.st.update(want='run', argv=argv, log=log, info=info, restarts=0, env=env or {})
            self._spawn()
            return self.status()

    def _spawn(self):
        os.makedirs(os.path.dirname(self.st['log']), exist_ok=True)
        out = open(self.st['log'], 'a')
        self.proc = subprocess.Popen(self.st['argv'], stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     env=dict(os.environ, **(self.st.get('env') or {})), start_new_session=True)
        out.close()
        self.st.update(state='running', pid=self.proc.pid, started=time.time(), ended=None, exit=None)
        self.save()
        threading.Thread(target=self._watch, args=(self.proc,), daemon=True).start()

    def _watch(self, proc):
        rc = proc.wait()
        with _lock:
            if proc is not self.proc:
                return
            self.st.update(exit=rc, ended=time.time(), pid=None)
            if getattr(self, 'closing', False):           # web.py ends: keep it marked as running, it resumes next time
                self.save()
                return
            if self.st['want'] != 'run':
                self.st['state'] = 'stopped'
            elif rc == 0:
                self.st.update(state='done', want='stop')
            elif self.restart and self.st['restarts'] < MAX_RESTARTS:
                self.st['restarts'] += 1
                self.st['state'] = 'restarting'
                self.save()
                t = threading.Timer(3, self._restart_if_wanted)
                t.daemon = True
                t.start()
                return
            else:
                self.st.update(state='failed', want='stop')
            self.save()
        if self.on_exit:
            self.on_exit(self)

    def _restart_if_wanted(self):
        with _lock:
            if self.st['want'] == 'run' and not self.running():
                self._spawn()

    def stop(self, keep_want=False):
        """Stop the process group (scan-device.sh and its zscan, or make-case and its zdb)."""
        with _lock:
            if keep_want:
                self.closing = True
            else:
                self.st['want'] = 'stop'
            proc = self.proc
            self.save()
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return self.status()

    def resume(self):
        """At the start of web.py: a job that was running when web.py ended is started again (restart jobs) or
        reported as interrupted."""
        if self.st.get('state') not in ('running', 'restarting'):
            return
        pid = self.st.get('pid')
        if _alive(pid):                                       # left over from a web.py that did not stop it
            try:
                os.killpg(pid, signal.SIGTERM)
            except OSError:
                pass
        if self.restart and self.st.get('want') == 'run' and self.st.get('argv'):
            self._spawn()
        else:
            self.st.update(state='interrupted', want='stop', pid=None)
            self.save()


def job(name, restart=False, on_exit=None):
    if name not in JOBS:
        JOBS[name] = Job(name, restart, on_exit)
    return JOBS[name]


def tail(path, n=12):
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 8192))
            return f.read().decode('utf-8', 'replace').splitlines()[-n:]
    except OSError:
        return []


def shutdown():
    """web.py ends: stop every child, keep what the user wanted so it resumes."""
    for j in list(JOBS.values()):
        if j.running():
            j.stop(keep_want=True)


def log_path(name):
    return f'{LOGS}/{name}.log'
