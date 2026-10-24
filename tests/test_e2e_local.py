"""The whole flow on one machine, through the web API, without ssh:
scan-device.sh -> rank-trees.py -> catalog rebuild -> select -> transfer -> verification.

The pool is a fixture image attached as a read-only file; there is no zdb involved (the case is written from
the fixture's sidecar). A fake ssh that always fails sits in PATH: any step that tried to use ssh would break.
"""
import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import time
import unittest
import urllib.request

import fx  # noqa: I001 - first: puts scripts/ on the path
from test_visible_errors import env_for


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def api(port, path, body=None):
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=None if body is None else json.dumps(body).encode(),
                                 method='GET' if body is None else 'POST')
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def wait(fn, what, seconds=120):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            v = fn()
        except OSError:                      # the server is not listening yet
            v = None
        if v:
            return v
        time.sleep(0.5)
    raise AssertionError(f'timeout waiting for {what}')


@unittest.skipUnless(fx.have_images('modern-single') and fx.ensure_built(), 'needs gcc and the modern-single images')
class EndToEndOnOneMachine(unittest.TestCase):
    def test_flow(self):
        variant = 'modern-single'
        side = fx.sidecars()[variant]
        work = fx.tmpdir()
        proc = None
        try:
            dev = fx.unpack(variant, os.path.join(work, 'img'))[0]
            os.chmod(dev, os.stat(dev).st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))   # "read-only"
            raw = os.path.join(work, 'raw')
            names = sorted({f['path'].split('/')[1] for f in side['files']})
            case = {'name': 'e2e', 'pool': {'name': side['pool'], 'devices': [{'path': dev, 'vdev': 0}]},
                    'profile': {'label_start': side['label_start'], 'compression': [], 'checksum': []},
                    'timeline': [{'txg': side['newest_txg'], 'time': 1}], 'compatibility': [],
                    'target': {'dataset': side['target'], 'root_names': names},
                    'scan': {'device': 'd0', 'chunk_dir': raw,
                             'log': os.path.join(work, 'scan.log')}}
            cpath = os.path.join(work, 'case.json')
            with open(cpath, 'w') as f:
                json.dump(case, f)
            env = env_for(os.path.join(work, 'state'), 'down', ZFS_SALVAGE_CASE=cpath,
                          ZFS_SALVAGE_CATALOG_DIR=os.path.join(work, 'catalog'))
            del env['ZFS_SALVAGE_HOST']                       # the default must be "local": the device exists here
            env.pop('ZFS_SALVAGE_READER', None)               # and the tools come from the repository

            r = subprocess.run(['bash', os.path.join(fx.SCRIPTS, 'scan-device.sh'), dev, '64', raw], env=env,
                               capture_output=True, text=True, timeout=300)
            self.assertEqual(r.returncode, 0, r.stdout[-500:])
            self.assertIn('ALL CHUNKS: 1/1', r.stdout)
            self.assertNotIn('ssh', r.stdout.replace('over ssh', ''))

            r = subprocess.run([sys.executable, '-I', os.path.join(fx.SCRIPTS, 'rank-trees.py')], env=env,
                               capture_output=True, text=True, timeout=300)
            self.assertEqual(r.returncode, 0, r.stderr[-500:])
            with open(os.path.join(work, 'state', 'status', 'target_candidates.json')) as f:
                self.assertTrue(json.load(f), 'rank-trees found no state of the target')

            port = free_port()
            proc = subprocess.Popen([sys.executable, '-I', os.path.join(fx.SCRIPTS, 'web.py')],
                                    env=dict(env, SALVAGE_UI_PORT=str(port)), stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            wait(lambda: api(port, '/api/workflow'), 'the web server')
            self.assertTrue(api(port, '/api/workflow')['host']['local'])
            self.assertEqual(api(port, '/api/workflow')['alerts'] if 'alerts' in api(port, '/api/workflow') else [], [])

            self.assertEqual(api(port, '/api/catalog/rebuild', {})['state'], 'running')
            job = wait(lambda: (lambda j: j if j['state'] in ('done', 'failed') else None)(api(port, '/api/catalog/status')),
                       'the catalog rebuild')
            self.assertEqual(job['state'], 'done', job)
            summary = api(port, '/api/summary')
            files = [f for f in side['files'] if f['type'] == 'f']
            self.assertEqual(summary['files'], len(files))

            everything = {'q': '', 'cat': '', 'from': '', 'to': '', 'min': '', 'under': '/', 'nohidden': ''}
            self.assertEqual(api(port, '/api/select', {'op': 'add', 'filter': everything})['sel_files'], len(files))

            dest = os.path.join(work, 'out')
            self.assertTrue(api(port, '/api/transfer/check', {'type': 'local', 'dest': dest})['ok'])
            api(port, '/api/transfer/start', {'type': 'local', 'dest': dest})
            st = wait(lambda: next((s for s in api(port, '/api/transfer/status')
                                    if s['state'] == 'done' and s.get('verify', {}).get('state') == 'done'), None),
                      'the transfer and its verification')
            self.assertEqual((st['verify']['bad_n'], st['verify']['ok']), (0, len(files)))
            for f in files:
                with open(os.path.join(dest, f['path'].lstrip('/')), 'rb') as fh:
                    self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), f['sha256'], f['path'])
            # the step bar can say "done" for all three steps
            wf = api(port, '/api/workflow')
            self.assertTrue(wf['transfer']['done'])
        finally:
            if proc:
                proc.terminate()
                proc.wait(timeout=10)
            fx.rmtree(work)


if __name__ == '__main__':
    unittest.main()
