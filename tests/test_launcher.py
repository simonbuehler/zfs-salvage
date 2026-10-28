"""./zfs-salvage: version, the sudo message, doctor, bundle-info, and that everything lands in ./data."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

import fx  # noqa: I001 - first: puts scripts/ on the path
from test_visible_errors import TOKEN, authed

LAUNCHER = os.path.join(fx.REPO, 'zfs-salvage')


def run(*args, env=None, timeout=120):
    e = dict(os.environ)
    for k in ('ZFS_SALVAGE_STATE_DIR', 'ZFS_SALVAGE_CATALOG_DIR', 'ZFS_SALVAGE_CASES_DIR', 'ZFS_SALVAGE_CASE'):
        e.pop(k, None)
    e.update(env or {})
    return subprocess.run([LAUNCHER, *args], capture_output=True, text=True, env=e, timeout=timeout)


class Launcher(unittest.TestCase):
    def test_version(self):
        r = run('version')
        self.assertEqual(r.returncode, 0)
        self.assertRegex(r.stdout.strip(), r'^zfs-salvage \S+')

    def test_version_from_a_bundle_file(self):
        d = tempfile.mkdtemp()
        try:
            for sub in ('scripts',):
                shutil.copytree(os.path.join(fx.REPO, sub), os.path.join(d, sub), ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copy(LAUNCHER, d)
            with open(os.path.join(d, 'VERSION'), 'w') as f:
                f.write('0.1.0-test\n')
            r = subprocess.run([os.path.join(d, 'zfs-salvage'), 'version'], capture_output=True, text=True)
            self.assertEqual(r.stdout.strip(), 'zfs-salvage 0.1.0-test')
        finally:
            fx.rmtree(d)

    @unittest.skipIf(os.geteuid() == 0, 'this test is about not being root')
    def test_without_root_it_says_so_and_stops(self):
        r = run()
        self.assertEqual(r.returncode, 1)
        self.assertIn('run with sudo', r.stdout)
        self.assertIn('sudo ./zfs-salvage', r.stdout)

    def test_unknown_command(self):
        r = run('frobnicate')
        self.assertEqual(r.returncode, 2)

    def test_bundle_info_lists_what_is_included(self):
        r = run('bundle-info', env={'ZFS_SALVAGE_STATE_DIR': tempfile.gettempdir() + '/zs-data-x'})
        self.assertIn('included:     scripts/zsread.py', r.stdout)
        self.assertIn('data:         ' + tempfile.gettempdir() + '/zs-data-x', r.stdout)

    def test_help_lists_the_commands_the_readme_uses(self):
        out = run('help').stdout
        for sub in ('doctor', 'web', 'make-case', 'scan', 'rank', 'version', 'bundle-info'):
            self.assertIn(sub, out)

    def test_scan_without_a_device_says_how_to_use_it(self):
        r = run('scan', env={'ZFS_SALVAGE_STATE_DIR': tempfile.gettempdir() + '/zs-data-x'})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('usage: scan-device.sh', r.stderr)

    @unittest.skipUnless(fx.have_images('modern-single') and fx.ensure_built(), 'needs gcc and the modern-single images')
    def test_scan_logs_where_the_status_page_looks(self):
        """The case file names the scan log logs/scan_<device>.log (make-case) – the launcher must write exactly there."""
        state, work = tempfile.mkdtemp(), fx.tmpdir()
        try:
            dev = fx.unpack('modern-single', work)[0]
            os.chmod(dev, 0o444)
            r = run('scan', dev, '64', env={'ZFS_SALVAGE_STATE_DIR': state})
            self.assertEqual(r.returncode, 0, r.stdout[-300:] + r.stderr[-300:])
            self.assertIn('ALL CHUNKS: 1/1', r.stdout)
            log = os.path.join(state, 'logs', 'scan_' + os.path.basename(dev) + '.log')
            with open(log) as f:
                self.assertIn('ALL CHUNKS: 1/1', f.read())
            self.assertTrue(os.path.isdir(os.path.join(state, 'logs', 'raw', 'scan_' + os.path.basename(dev))))
        finally:
            fx.rmtree(state)
            fx.rmtree(work)

    def test_rank_and_make_case_use_the_data_directory(self):
        state = tempfile.mkdtemp()
        try:
            r = run('rank', env={'ZFS_SALVAGE_STATE_DIR': state})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(os.path.exists(os.path.join(state, 'status', 'target_candidates.json')))
            r = run('make-case', '--help', env={'ZFS_SALVAGE_STATE_DIR': state})
            self.assertEqual(r.returncode, 0)
            self.assertIn('--dev', r.stdout)
            self.assertTrue(os.path.isdir(os.path.join(state, 'cases')))
        finally:
            fx.rmtree(state)

    def test_doctor_runs_through_the_launcher(self):
        state = tempfile.mkdtemp()
        try:
            r = run('doctor', '--json', '--no-root', env={'ZFS_SALVAGE_STATE_DIR': state})
            checks = {c['check'] for c in json.loads(r.stdout)}
            self.assertTrue({'root', 'zdb', 'zscan', 'data directory', 'case file'} <= checks)
        finally:
            fx.rmtree(state)

    def test_web_started_by_the_launcher_has_a_session_token_in_its_link(self):
        state = tempfile.mkdtemp()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        e = {k: v for k, v in os.environ.items() if not k.startswith('ZFS_SALVAGE_')}
        e.update(ZFS_SALVAGE_STATE_DIR=state, SALVAGE_UI_PORT=str(port))
        p = subprocess.Popen([LAUNCHER, 'web'], env=e, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            link = p.stderr.readline().strip()
            self.assertIn(f'http://localhost:{port}/?t=', link)
            token = link.split('?t=')[1].split()[0]
            for _ in range(60):
                try:
                    req = urllib.request.Request(f'http://127.0.0.1:{port}/api/workflow', headers={'X-ZS-Token': token})
                    self.assertIn('transfer', json.load(urllib.request.urlopen(req, timeout=3)))
                    break
                except OSError:
                    time.sleep(0.3)
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(f'http://127.0.0.1:{port}/api/workflow', timeout=3)
            self.assertEqual(cm.exception.code, 403)
        finally:
            p.terminate()
            p.wait(timeout=10)
            p.stderr.close()
            fx.rmtree(state)

    def test_web_keeps_everything_in_the_data_directory(self):
        state = tempfile.mkdtemp()
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        e = dict(os.environ, ZFS_SALVAGE_STATE_DIR=state, SALVAGE_UI_PORT=str(port), ZFS_SALVAGE_TOKEN=TOKEN)
        for k in ('ZFS_SALVAGE_CATALOG_DIR', 'ZFS_SALVAGE_CASES_DIR', 'ZFS_SALVAGE_CASE'):
            e.pop(k, None)
        p = subprocess.Popen([LAUNCHER, 'web'], env=e, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline, wf = time.time() + 30, None
            while time.time() < deadline and wf is None:
                try:
                    wf = json.load(urllib.request.urlopen(authed(f'http://127.0.0.1:{port}/api/workflow'), timeout=3))
                except OSError:
                    time.sleep(0.5)
            self.assertIsNotNone(wf, 'the web UI did not start')
            self.assertTrue(os.path.isdir(os.path.join(state, 'catalog')))
            self.assertTrue(os.path.isdir(os.path.join(state, 'cases')))
            deadline = time.time() + 30
            while time.time() < deadline and not os.path.exists(os.path.join(state, 'status', 'index.html')):
                time.sleep(0.5)
            self.assertTrue(os.path.exists(os.path.join(state, 'status', 'index.html')), 'status page not in the data directory')
            # regression: generating the status page used to create an empty catalog file, which broke the API for good
            self.assertFalse(os.path.exists(os.path.join(state, 'catalog', 'catalog.sqlite')))
            self.assertIn('alerts', json.load(urllib.request.urlopen(authed(f'http://127.0.0.1:{port}/api/workflow'), timeout=5)) | {'alerts': 0})
            try:
                urllib.request.urlopen(authed(f'http://127.0.0.1:{port}/api/summary'), timeout=5)
                self.fail('/api/summary should say there is no catalog')
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 404)
        finally:
            p.terminate()
            p.wait(timeout=10)
            fx.rmtree(state)


if __name__ == '__main__':
    unittest.main()
