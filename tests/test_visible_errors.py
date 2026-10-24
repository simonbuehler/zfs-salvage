"""Problems that used to be silent are visible: unreachable recovery host, library not used (slow
mode), a scan that cannot start. Uses tests/bin/ssh, a fake ssh, so no host is contacted."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

import fx  # noqa: I001 - first: puts scripts/ on the path

FAKE_BIN = os.path.join(fx.TESTS, 'bin')
EXAMPLE_CASE = os.path.join(fx.REPO, 'cases', 'example.json')


def env_for(state, mode, **extra):
    e = dict(os.environ, PATH=FAKE_BIN + os.pathsep + os.environ['PATH'], FAKE_SSH=mode,
             ZFS_SALVAGE_HOST='fakehost', ZFS_SALVAGE_STATE_DIR=state, ZFS_SALVAGE_CASE=EXAMPLE_CASE)
    e.update(extra)
    return e


def run_status_page(state, mode, **extra):
    r = subprocess.run([sys.executable, '-I', os.path.join(fx.SCRIPTS, 'status_page.py')],
                       env=env_for(state, mode, **extra), capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-500:]
    with open(os.path.join(state, 'status', 'index.html'), encoding='utf-8') as f:
        return f.read()


class SshErrors(unittest.TestCase):
    def call(self, mode):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import common; "
                "out = common.ssh('echo hi'); print(repr(out), repr(common.ssh_error()))")
        r = subprocess.run([sys.executable, '-I', '-c', code, fx.SCRIPTS], env=env_for(tempfile.gettempdir(), mode),
                           capture_output=True, text=True, timeout=60)
        return r.stdout.strip()

    def test_down_is_reported(self):
        self.assertEqual(self.call('down'), "'' 'ssh: connect to host fake port 22: Connection refused'")

    def test_remote_command_failure_is_not_a_connection_problem(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import common; "
                "print(repr(common.ssh('echo out; exit 3')), repr(common.ssh_error()))")
        r = subprocess.run([sys.executable, '-I', '-c', code, fx.SCRIPTS], env=env_for(tempfile.gettempdir(), 'local'),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.stdout.strip(), "'out\\n' None")


class StatusPage(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix='zfs-salvage-state-')

    def tearDown(self):
        fx.rmtree(self.state)

    def test_unreachable_host_is_shown(self):
        page = run_status_page(self.state, 'down')
        self.assertIn('Recovery host fakehost is unreachable: ssh: connect to host fake port 22: Connection refused', page)
        with open(os.path.join(self.state, 'status', 'workflow.json')) as f:
            self.assertIn('unreachable', f.read())

    def test_reachable_host_shows_no_alert(self):
        page = run_status_page(self.state, 'local')
        self.assertNotIn('class="alert', page)

    def test_slow_mode_is_shown(self):
        os.makedirs(os.path.join(self.state, 'status'))
        with open(os.path.join(self.state, 'status', 'probe_stats.json'), 'w') as f:
            json.dump({'probed': 1, 'pending': 0, 'by_dataset': {}, 'slow_mode': True,
                       'slow_reason': 'zsread: SLOW MODE - libzsalvage.so not found (looked next to zsread.py)'}, f)
        page = run_status_page(self.state, 'local')
        self.assertIn('Slow mode', page)
        self.assertIn('libzsalvage.so not found', page)

    def test_scan_error_line_is_shown(self):
        with open(EXAMPLE_CASE) as f:
            case = json.load(f)
        log_rel = os.path.relpath(os.path.join(self.state, 'scan.log'), fx.REPO)
        case['scan']['log'] = log_rel
        path = os.path.join(self.state, 'case.json')
        with open(path, 'w') as f:
            json.dump(case, f)
        with open(os.path.join(self.state, 'scan.log'), 'w') as f:
            f.write('ro_sdb1: 1000 bytes, 1 chunks\nERROR: zscan is not installed on fakehost – run scripts/deploy-tools.sh\n')
        page = run_status_page(self.state, 'local', ZFS_SALVAGE_CASE=path)
        self.assertIn('Scan: zscan is not installed on fakehost', page)


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class WebServer(unittest.TestCase):
    def test_status_page_of_a_running_server_shows_the_unreachable_host_within_30_s(self):
        """Acceptance: dev instance on its own port, host that cannot be reached."""
        state, port = tempfile.mkdtemp(prefix='zfs-salvage-state-'), free_port()
        proc = subprocess.Popen([sys.executable, '-I', os.path.join(fx.SCRIPTS, 'web.py')],
                                env=env_for(state, 'down', SALVAGE_UI_PORT=str(port)),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline, text = time.time() + 30, ''
            while time.time() < deadline and 'is unreachable' not in text:
                time.sleep(1)
                try:
                    text = urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=5).read().decode()
                except OSError:
                    pass
            self.assertIn('is unreachable', text)
            wf = json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/api/workflow', timeout=5))
            self.assertTrue(wf['alerts'])
        finally:
            proc.terminate()
            proc.wait(timeout=10)
            fx.rmtree(state)


@unittest.skipUnless(fx.have_images('legacy-single') and fx.ensure_built(), 'needs gcc and the legacy-single images')
class SlowModeChain(unittest.TestCase):
    """library missing -> zsread says so -> rank-trees records it -> the status page shows it."""

    @classmethod
    def setUpClass(cls):
        cls.dir = fx.tmpdir()
        cls.dev = fx.unpack('legacy-single', os.path.join(cls.dir, 'img'))[0]
        side = fx.sidecars()['legacy-single']
        tg = side['datasets'][side['target']]
        cls.cand = f"OBJSET {side['label_start'] + tg['dvas'][0]['offset']:x} lzjb {tg['psize']} {tg['lsize']} {tg['birth']}\n"
        cls.side = side

    @classmethod
    def tearDownClass(cls):
        fx.rmtree(cls.dir)

    def zsread_dir(self, with_lib):
        d = tempfile.mkdtemp(dir=self.dir)
        shutil.copy(os.path.join(fx.SCRIPTS, 'zsread.py'), d)
        if with_lib:
            shutil.copy(fx.LIB, d)
        return d

    def batch(self, with_lib):
        d = self.zsread_dir(with_lib)
        r = subprocess.run([sys.executable, '-I', os.path.join(d, 'zsread.py'), 'batch'], input=self.cand,
                           env=dict(os.environ, ZSREAD_DEVS=self.dev, ZSREAD_LABEL_START=str(self.side['label_start'])),
                           capture_output=True, text=True, timeout=120)
        return json.loads(r.stdout.splitlines()[0]), r.stderr

    def test_zsread_reports_slow_mode(self):
        res, err = self.batch(with_lib=False)
        self.assertTrue(res['ok'])
        self.assertIs(res.get('lib'), False)
        self.assertIn('SLOW MODE - libzsalvage.so not found', err)

    def test_zsread_with_library_is_quiet(self):
        res, err = self.batch(with_lib=True)
        self.assertTrue(res['ok'])
        self.assertNotIn('lib', res)
        self.assertEqual(err, '')

    def test_broken_library_is_reported_with_the_reason(self):
        d = self.zsread_dir(False)
        with open(os.path.join(d, 'libzsalvage.so'), 'wb') as f:
            f.write(b'not an elf file')
        r = subprocess.run([sys.executable, '-I', os.path.join(d, 'zsread.py'), 'batch'], input='',
                           env=dict(os.environ, ZSREAD_DEVS=self.dev), capture_output=True, text=True, timeout=60)
        self.assertIn('cannot be loaded', r.stderr)

    def test_whole_chain(self):
        """rank-trees (real script) talks to a 'host' that runs zsread without its library."""
        state = os.path.join(self.dir, 'state')
        raw = os.path.join(self.dir, 'raw')
        os.makedirs(raw)
        import gzip
        scan = subprocess.run([fx.ZSCAN, self.dev], capture_output=True, text=True).stdout
        with gzip.open(os.path.join(raw, 'chunk_0000.tsv.gz'), 'wt') as f:
            f.write(scan)
        open(os.path.join(raw, 'chunk_0000.tsv.gz.done'), 'w').close()
        case = {'name': 't', 'pool': {'name': 'p', 'devices': [{'path': self.dev, 'vdev': 0}]},
                'profile': {'label_start': self.side['label_start'], 'meta_dnode_levels': 6,
                            'compression': [], 'checksum': []},
                'timeline': [{'txg': 100, 'time': 1}], 'target': {'dataset': 'p/home', 'root_names': ['docs']},
                'scan': {'device': 'd0', 'chunk_dir': os.path.relpath(raw, fx.REPO), 'log': 'x.log'}}
        cpath = os.path.join(self.dir, 'case.json')
        with open(cpath, 'w') as f:
            json.dump(case, f)
        reader = os.path.join(self.zsread_dir(False), 'zsread.py')
        e = env_for(state, 'local', ZFS_SALVAGE_CASE=cpath, ZFS_SALVAGE_READER=reader)
        os.makedirs(os.path.join(state, 'status'))
        r = subprocess.run([sys.executable, '-I', os.path.join(fx.SCRIPTS, 'rank-trees.py'), '5'], env=e,
                           capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        with open(os.path.join(state, 'status', 'probe_stats.json')) as f:
            stats = json.load(f)
        self.assertTrue(stats['slow_mode'])
        self.assertIn('libzsalvage.so not found', stats['slow_reason'])
        page = run_status_page(state, 'local', ZFS_SALVAGE_CASE=cpath, ZFS_SALVAGE_READER=reader)
        self.assertIn('Slow mode', page)


class ScanPreflight(unittest.TestCase):
    def run_scan(self, mode):
        out = tempfile.mkdtemp(prefix='zfs-salvage-scan-')
        try:
            r = subprocess.run(['bash', os.path.join(fx.SCRIPTS, 'scan-device.sh'), '/dev/null', '64', out],
                               env=env_for(out, mode), capture_output=True, text=True, timeout=120)
            return r.returncode, r.stdout
        finally:
            fx.rmtree(out)

    def test_host_unreachable(self):
        rc, out = self.run_scan('down')
        self.assertEqual(rc, 1)
        self.assertIn('ERROR: cannot reach fakehost over ssh: ssh: connect to host fake port 22: Connection refused', out)

    def test_zscan_missing(self):
        rc, out = self.run_scan('nozscan')
        self.assertEqual(rc, 1)
        self.assertIn('ERROR: zscan is not installed on fakehost', out)

    def test_zscan_too_old(self):
        rc, out = self.run_scan('oldzscan')
        self.assertEqual(rc, 1)
        self.assertIn('ERROR: zscan on fakehost is too old', out)


if __name__ == '__main__':
    unittest.main()
