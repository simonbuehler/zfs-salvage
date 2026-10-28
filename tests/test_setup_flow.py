"""Step 0 "Set up" and job control.

Acceptance: from an empty data directory to verified rescued files only through the HTTP API of steps 0-3 – the only command
is starting the launcher. zdb is replaced by recorded real zdb output (tests/bin/zdb-recorded), everything else is real.
"""
import hashlib
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import fx  # noqa: I001 - first: puts scripts/ on the path

LAUNCHER = os.path.join(fx.REPO, 'zfs-salvage')
TOKEN = 'setup-test-token'
EVERYTHING = {'q': '', 'cat': '', 'from': '', 'to': '', 'min': '', 'under': '/', 'nohidden': ''}


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, state, port):
        self.state, self.port = state, port
        env = {k: v for k, v in os.environ.items() if not k.startswith('ZFS_SALVAGE_')}
        env.update(ZFS_SALVAGE_STATE_DIR=state, ZFS_SALVAGE_TOKEN=TOKEN, SALVAGE_UI_PORT=str(port),
                   ZFS_SALVAGE_ZDB=os.path.join(fx.TESTS, 'bin', 'zdb-recorded'), ZDB_RECORDED='modern-single-d0',
                   ZFS_SALVAGE_SMARTCTL='/nonexistent/smartctl', ZFS_SALVAGE_DOCTOR_FAKE=json.dumps({'lsblk': ''}),
                   ZFS_SALVAGE_ALLOW_RAM_TARGET='1')
        self.proc = subprocess.Popen([LAUNCHER, 'web'], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.wait(lambda: self.get('/api/workflow'), 'the web server')

    def call(self, method, path, body=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=60)
        data = None if body is None else json.dumps(body).encode()
        c.request(method, path, body=data, headers={'Host': f'localhost:{self.port}', 'X-ZS-Token': TOKEN})
        r = c.getresponse()
        out = r.read()
        c.close()
        if r.status != 200:
            raise AssertionError(f'{method} {path} -> {r.status} {out[:200]!r}')
        return json.loads(out)

    def get(self, path):
        return self.call('GET', path)

    def post(self, path, body=None):
        return self.call('POST', path, body or {})

    def wait(self, fn, what, seconds=180):
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                v = fn()
            except (OSError, AssertionError, http.client.HTTPException, ValueError):
                v = None                                # not listening (yet, or restarting)
            if v:
                return v
            time.sleep(0.5)
        raise AssertionError(f'timeout waiting for {what}')

    def job(self, name):
        return self.get('/api/setup/state')['jobs'][name]

    def close(self):
        self.proc.terminate()
        self.proc.wait(timeout=20)


@unittest.skipUnless(fx.have_images('modern-single') and fx.ensure_built(), 'needs gcc and the modern-single images')
class SetupThroughTheApi(unittest.TestCase):
    def test_from_nothing_to_rescued_files(self):
        work = fx.tmpdir()
        srv = None
        try:
            img = fx.unpack('modern-single', os.path.join(work, 'img'))[0]
            fx.damage(os.path.join(work, 'img'), 'modern-single')         # the target's objset block is destroyed
            state = os.path.join(work, 'data')
            srv = Server(state, free_port())

            # step 0: checks and devices
            checks = {c['check']: c for c in srv.get('/api/setup/doctor')}
            self.assertIn('zdb', checks)
            self.assertEqual(checks['case file']['status'], 'WARN')
            dev = srv.get('/api/setup/devices?extra=' + img)
            self.assertEqual([(d['path'], d['read_only']) for d in dev], [(img, False)])
            self.assertEqual(srv.post('/api/setup/probe', {'devs': [img]})['state'], 'refused', 'a writable device is not examined')
            self.assertTrue(srv.post('/api/setup/readonly', {'dev': img})['ok'])

            # examine the pool, then create the case for the proposed (the only unreadable) dataset
            srv.post('/api/setup/probe', {'devs': [img]})
            st = srv.wait(lambda: (lambda s: s if s['probe'] and s['jobs']['probe']['state'] == 'done' else None)(
                srv.get('/api/setup/state')), 'the examination')
            p = st['probe']
            self.assertEqual(p['pool'], 'fx-modern-single')
            self.assertEqual(p['proposed_target'], 'fx-modern-single/home')
            self.assertFalse(next(d for d in p['datasets'] if d['name'] == p['proposed_target'])['readable'])
            srv.post('/api/setup/case', {'devs': p['devices'], 'target': p['proposed_target'], 'root_names': ''})
            # the server restarts itself with the new case
            case = srv.wait(lambda: srv.get('/api/setup/state')['case'], 'the case after the restart')
            self.assertEqual((case['target'], case['diagnosis']['situation']), ('fx-modern-single/home', 'objset'))
            self.assertEqual(srv.proc.poll(), None, 'same process (exec), still running')

            # scan and rank as jobs of the web server; a second start does not start a second scan
            first = srv.post('/api/jobs/scan/start')
            again = srv.post('/api/jobs/scan/start')
            if first.get('state') == 'running' and again.get('state') == 'running':
                self.assertEqual(first['pid'], again['pid'])
            srv.wait(lambda: srv.job('scan')['state'] == 'done', 'the scan')
            srv.wait(lambda: srv.job('rank')['state'] == 'done' and srv.get('/api/target/groups')['groups'], 'the ranking')
            self.assertTrue(os.path.exists(os.path.join(state, 'logs', 'scan_d0.img.log')), 'the scan logs where the status page reads')

            # steps 1-3 as before: choose the file system, catalog, select, transfer, verify
            group = next(g for g in srv.get('/api/target/groups')['groups'] if 'docs' in g['names'])
            srv.post('/api/target/choose', {'names': group['names']})
            srv.wait(lambda: srv.get('/api/workflow').get('newest', {}).get('txg'), 'the candidates of the chosen group', 120)
            self.assertEqual(srv.post('/api/catalog/rebuild')['state'], 'running')
            srv.wait(lambda: srv.get('/api/catalog/status')['state'] == 'done', 'the catalog')
            files = [f for f in fx.sidecars()['modern-single']['files'] if f['type'] == 'f']
            self.assertEqual(srv.post('/api/select', {'op': 'add', 'filter': EVERYTHING})['sel_files'], len(files))
            dest = os.path.join(work, 'rescued')
            srv.post('/api/transfer/start', {'type': 'local', 'dest': dest})
            done = srv.wait(lambda: next((t for t in srv.get('/api/transfer/status') if t['state'] == 'done'
                                          and (t.get('verify') or {}).get('state') == 'done'), None), 'the transfer')
            self.assertEqual((done['verify']['ok'], done['verify']['bad_n']), (len(files), 0))
            for f in files:
                with open(os.path.join(dest, f['path'].lstrip('/')), 'rb') as fh:
                    self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), f['sha256'], f['path'])
            w = srv.get('/api/workflow')
            self.assertTrue(w['setup']['case'])
            self.assertTrue(w['transfer']['done'])
        finally:
            if srv:
                srv.close()
            fx.rmtree(work)


class JobControl(unittest.TestCase):
    """jobs.py on its own: restart after a crash, one job at a time, resume after web.py ended."""

    def run_py(self, code, state):
        env = {k: v for k, v in os.environ.items() if not k.startswith('ZFS_SALVAGE_')}
        env['ZFS_SALVAGE_STATE_DIR'] = state
        r = subprocess.run([sys.executable, '-I', '-c', 'import sys; sys.path.insert(0, sys.argv[1]); import jobs, time\n' + code,
                            fx.SCRIPTS], capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        return r.stdout

    def test_crash_is_restarted_three_times_then_failed(self):
        with tempfile.TemporaryDirectory() as d:
            out = self.run_py("j = jobs.job('x', restart=True)\nj.start(['bash', '-c', 'echo run; exit 3'], jobs.log_path('x'))\n"
                              "time.sleep(14)\nprint(j.status()['state'], j.status()['restarts'])", d)
            self.assertEqual(out.split(), ['failed', '3'])
            with open(os.path.join(d, 'logs', 'x.log')) as f:
                self.assertEqual(f.read().count('run'), 4)

    def test_clean_exit_is_done_and_not_restarted(self):
        with tempfile.TemporaryDirectory() as d:
            out = self.run_py("j = jobs.job('x', restart=True)\nj.start(['true'], jobs.log_path('x'))\ntime.sleep(4)\nprint(j.status()['state'], j.status()['restarts'])", d)
            self.assertEqual(out.split(), ['done', '0'])

    def test_one_at_a_time_also_across_processes(self):
        with tempfile.TemporaryDirectory() as d:
            out = self.run_py("j = jobs.job('x')\na = j.start(['sleep', '30'], jobs.log_path('x'))\nb = j.start(['sleep', '30'], jobs.log_path('x'))\n"
                              "print(a['pid'] == b['pid'])\n"
                              "import importlib\nother = jobs.Job('x')\nc = other.start(['sleep', '30'], jobs.log_path('x'))\n"
                              "print(c['info'])\nj.stop()\nprint(j.status()['state'])", d)
            lines = out.splitlines()
            self.assertEqual(lines[0], 'True')
            self.assertEqual(lines[1], 'already running in another process')
            self.assertEqual(lines[2], 'stopped')

    def test_resume_after_web_ended(self):
        with tempfile.TemporaryDirectory() as d:
            out = self.run_py("j = jobs.job('scan', restart=True)\nj.start(['sleep', '60'], jobs.log_path('scan'))\npid = j.st['pid']\n"
                              "jobs.shutdown()\nprint(pid)", d)
            old = int(out.split()[0])
            with open(os.path.join(d, 'jobs', 'scan.json')) as f:
                saved = json.load(f)
            self.assertEqual((saved['state'], saved['want']), ('running', 'run'), 'ended with web.py, still wanted')
            out = self.run_py("j = jobs.job('scan', restart=True)\nj.resume()\nprint(j.status()['state'], j.status()['pid'])\nj.stop()", d)
            state, pid = out.split()
            self.assertEqual(state, 'running')
            self.assertNotEqual(int(pid), old)
            # a job that is not restarted on its own is reported as interrupted
            with open(os.path.join(d, 'jobs', 'case.json'), 'w') as f:
                json.dump({'name': 'case', 'state': 'running', 'want': 'run', 'pid': None, 'argv': ['true'], 'log': d + '/l'}, f)
            out = self.run_py("j = jobs.job('case')\nj.resume()\nprint(j.status()['state'])", d)
            self.assertEqual(out.strip(), 'interrupted')


class FailingDiskInTheUi(unittest.TestCase):
    """In the UI: a failing disk is scanned only after the user said yes."""

    def test_scan_asks_first(self):
        with tempfile.TemporaryDirectory() as d:
            case = os.path.join(d, 'case.json')
            with open(case, 'w') as f:
                json.dump({'name': 'c', 'pool': {'name': 'p', 'devices': [{'path': '/dev/fake1', 'vdev': 0}]},
                           'target': {'dataset': 'p/h', 'root_names': []}, 'scan': {'device': 'fake1'}}, f)
            env = {k: v for k, v in os.environ.items() if not k.startswith('ZFS_SALVAGE_')}
            env.update(ZFS_SALVAGE_STATE_DIR=d, ZFS_SALVAGE_CASE=case, FAKE_SMART='pending',
                       ZFS_SALVAGE_SMARTCTL=os.path.join(fx.TESTS, 'bin', 'smartctl-fake'),
                       ZFS_SALVAGE_DOCTOR_FAKE=json.dumps({'disks': {'/dev/fake1': ['/dev/diskA']}}))
            code = ('import json, sys; sys.path.insert(0, sys.argv[1]); import setup_api; '
                    'print(json.dumps(setup_api.start_scan({})))')
            r = subprocess.run([sys.executable, '-I', '-c', code, fx.SCRIPTS], capture_output=True, text=True, env=env, timeout=60)
            res = json.loads(r.stdout)
            self.assertEqual(res['state'], 'needs_confirmation')
            self.assertIn('This disk is failing', res['info'])
            self.assertFalse(os.path.exists(os.path.join(d, 'jobs', 'scan.json')), 'nothing was started')


if __name__ == '__main__':
    unittest.main()
