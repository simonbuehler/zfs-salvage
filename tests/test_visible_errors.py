"""Problems that used to be silent are visible: library not used (slow
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


TOKEN = 'test-token'                 # what the web server of the tests expects (X-ZS-Token header)


def env_for(state, mode, **extra):
    e = dict(os.environ, PATH=FAKE_BIN + os.pathsep + os.environ['PATH'], FAKE_SSH=mode, ZFS_SALVAGE_TOKEN=TOKEN,
             ZFS_SALVAGE_STATE_DIR=state, ZFS_SALVAGE_CASE=EXAMPLE_CASE)
    e.update(extra)
    return e


def authed(url):
    """A request for the web server of the tests, with the session token."""
    return urllib.request.Request(url, headers={'X-ZS-Token': TOKEN})


def run_status_page(state, mode, **extra):
    r = subprocess.run([sys.executable, '-I', os.path.join(fx.SCRIPTS, 'status_page.py')],
                       env=env_for(state, mode, **extra), capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-500:]
    with open(os.path.join(state, 'status', 'index.html'), encoding='utf-8') as f:
        return f.read()


class StatusPage(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix='zfs-salvage-state-')

    def tearDown(self):
        fx.rmtree(self.state)

    def test_no_problem_shows_no_alert(self):
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
        log_rel = os.path.join(self.state, 'scan.log')
        case['scan']['log'] = log_rel
        path = os.path.join(self.state, 'case.json')
        with open(path, 'w') as f:
            json.dump(case, f)
        with open(os.path.join(self.state, 'scan.log'), 'w') as f:
            f.write('ro_sdb1: 1000 bytes, 1 chunks\nERROR: zscan is not built (tools/zscan/zscan)\n')
        page = run_status_page(self.state, 'local', ZFS_SALVAGE_CASE=path)
        self.assertIn('Scan: zscan is not built', page)


    def test_scan_progress_is_read_from_the_english_log(self):
        """Found while recording the demo: the page only understood the old German log ("Bytes, … Abschnitte à")."""
        with open(EXAMPLE_CASE) as f:
            case = json.load(f)
        raw = os.path.join(self.state, 'raw')
        os.makedirs(raw)
        case['scan'].update(log=os.path.join(self.state, 'scan.log'), chunk_dir=raw)
        path = os.path.join(self.state, 'case.json')
        with open(path, 'w') as f:
            json.dump(case, f)
        with open(os.path.join(self.state, 'scan.log'), 'w') as f:
            f.write('/dev/mapper/ro_sdb1: 268435456 bytes, 1 chunks of 64 GiB -> raw\n12:00 chunk 0/1 done: 693 finds, 1 s\nALL CHUNKS: 1/1\n')
        for name in ('chunk_0000.tsv.gz', 'chunk_0000.tsv.gz.done'):
            open(os.path.join(raw, name), 'w').close()
        page = run_status_page(self.state, 'local', ZFS_SALVAGE_CASE=path)
        self.assertIn('(268 MB)', page)
        self.assertIn('Scan complete</b> · 1 chunks', page)

def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]



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
                'scan': {'device': 'd0', 'chunk_dir': raw, 'log': 'x.log'}}
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
    def run_scan(self, zscan, dev='/dev/null'):
        out = tempfile.mkdtemp(prefix='zfs-salvage-scan-')
        try:
            r = subprocess.run(['bash', os.path.join(fx.SCRIPTS, 'scan-device.sh'), dev, '64', out],
                               env=env_for(out, 'down', ZFS_SALVAGE_ZSCAN=zscan), capture_output=True, text=True, timeout=120)
            return r.returncode, r.stdout
        finally:
            fx.rmtree(out)

    def fake_zscan(self, answer):
        d = tempfile.mkdtemp(prefix='zfs-salvage-zscan-')
        self.addCleanup(fx.rmtree, d)
        path = os.path.join(d, 'zscan')
        with open(path, 'w') as f:
            f.write(f'#!/bin/sh\necho "{answer}"\n')
        os.chmod(path, 0o755)
        return path

    def test_zscan_missing(self):
        rc, out = self.run_scan('/nonexistent/zscan')
        self.assertEqual(rc, 1)
        self.assertIn('ERROR: zscan is not built (/nonexistent/zscan)', out)

    def test_zscan_too_old(self):
        rc, out = self.run_scan(self.fake_zscan('unknown or incomplete option'))
        self.assertEqual(rc, 1)
        self.assertIn('ERROR: zscan is too old', out)

    def test_writable_device_is_refused(self):
        img = os.path.join(tempfile.mkdtemp(prefix='zfs-salvage-img-'), 'pool.img')
        self.addCleanup(fx.rmtree, os.path.dirname(img))
        with open(img, 'wb') as f:
            f.truncate(1 << 20)
        rc, out = self.run_scan(self.fake_zscan('ok'), img)
        self.assertEqual(rc, 1)
        self.assertIn('is not read-only – refusing to scan', out)


if __name__ == '__main__':
    unittest.main()
