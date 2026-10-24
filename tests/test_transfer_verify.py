"""Transfers are verified on the target, and a resume does not trust the manifest.

Background: on a real rescue one file was cut off on the target (zeros from byte 338,944, same size
and mtime) while the manifest said OK, because the manifest line is written when the source has read a file.
The transfer runs here through the fake ssh (tests/bin/ssh) against a fixture image, in separate processes so
that "the server died" is real.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import unittest

import fx  # noqa: I001 - first: puts scripts/ on the path
from test_visible_errors import env_for

DRIVER = os.path.join(fx.TESTS, 'transfer_driver.py')


@unittest.skipUnless(fx.have_images('modern-single') and fx.ensure_built(), 'needs gcc and the modern-single images')
class TransferVerify(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = fx.tmpdir()
        cls.dev = fx.unpack('modern-single', os.path.join(cls.dir, 'img'))[0]
        cls.side = fx.sidecars()['modern-single']
        zdir = os.path.join(cls.dir, 'zs')
        os.makedirs(zdir)
        shutil.copy(os.path.join(fx.SCRIPTS, 'zsread.py'), zdir)
        shutil.copy(fx.LIB, zdir)
        case = {'name': 't', 'pool': {'name': 'p', 'devices': [{'path': cls.dev, 'vdev': 0}]},
                'profile': {'label_start': cls.side['label_start']}, 'target': {'dataset': 'p/home', 'root_names': []}}
        cpath = os.path.join(cls.dir, 'case.json')
        with open(cpath, 'w') as f:
            json.dump(case, f)
        cls.env = env_for(os.path.join(cls.dir, 'state'), 'local', ZFS_SALVAGE_CASE=cpath,
                          ZFS_SALVAGE_READER=os.path.join(zdir, 'zsread.py'),
                          ZFS_SALVAGE_CATALOG_DIR=os.path.join(cls.dir, 'catalog'))
        tg = cls.side['datasets'][cls.side['target']]
        r = cls.driver('prepare', 'OBJSET', f"{cls.side['label_start'] + tg['dvas'][0]['offset']:x}", 'off',
                       str(tg['psize']), str(tg['lsize']))
        cls.nfiles = int(r.stdout.strip())
        cls.files = {f['path']: f for f in cls.side['files'] if f['type'] == 'f'}

    @classmethod
    def tearDownClass(cls):
        fx.rmtree(cls.dir)

    @classmethod
    def driver(cls, *args, expect=0):
        r = subprocess.run([sys.executable, '-I', DRIVER, *args], env=cls.env, capture_output=True, text=True, timeout=600)
        assert r.returncode == expect, f'{args}: exit {r.returncode}\n{r.stderr[-800:]}'
        return r

    def dest(self, name):
        d = os.path.join(self.dir, 'dest-' + name)
        shutil.rmtree(d, ignore_errors=True)
        return d

    def status(self, *args):
        return json.loads(self.driver(*args).stdout.strip().splitlines()[-1])

    def job_folder(self, dest):
        folder = os.path.join(self.dir, 'catalog', 'transfers')
        for j in os.listdir(folder):
            with open(os.path.join(folder, j, 'target.json')) as f:
                if json.load(f)['dest'] == dest:
                    return os.path.join(folder, j)
        raise AssertionError(f'no job for {dest}')

    def check_content(self, dest):
        for path, f in self.files.items():
            with open(os.path.join(dest, path.lstrip('/')), 'rb') as fh:
                self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), f['sha256'], path)

    def test_a_complete_transfer_is_verified_by_itself(self):
        d = self.dest('clean')
        st = self.status('run', 'local', d)
        self.assertEqual(st['state'], 'done')
        v = st['verify']
        self.assertEqual((v['state'], v['bad_n'], v['ok'], v['total']), ('done', 0, len(self.files), len(self.files)))
        self.check_content(d)

    def test_truncated_and_missing_files_are_found_and_fixed(self):
        d = self.dest('damaged')
        self.status('run', 'local', d)
        cut = os.path.join(d, 'photos/2013-12/img_0000.jpg')
        size = os.path.getsize(cut)
        with open(cut, 'r+b') as f:                       # what happened on the real rescue: zeros from the middle on
            f.seek(size // 3)
            f.write(bytes(size - size // 3))
        os.remove(os.path.join(d, 'docs/note-00.txt'))
        st = self.status('verify', 'local', d)
        v = st['verify']
        self.assertEqual((v['bad_n'], v['missing_n']), (2, 1))
        self.assertEqual(sorted(x.split(': ')[1] for x in v['bad_sample']), ['/docs/note-00.txt', '/photos/2013-12/img_0000.jpg'])
        self.assertEqual(os.path.getsize(cut), size)      # same size: only the hash can tell
        st = self.status('recopy', 'local', d)
        self.assertEqual(st['verify']['bad_n'], 0)
        self.assertEqual(st['verify']['state'], 'done')
        self.check_content(d)

    def test_a_killed_run_is_resumed_without_trusting_its_last_files(self):
        d = self.dest('killed')
        self.driver('run', 'local', d, '20', expect=7)    # the "server" dies after 20 files, no END marker
        with open(os.path.join(self.job_folder(d), 'manifest.tsv')) as f:
            lines = [l.split('\t') for l in f.read().splitlines()]
        self.assertTrue(any(l[:2] == ['R', 'START'] for l in lines))
        self.assertFalse(any(l[:2] == ['R', 'END'] for l in lines))
        first = [l[2] for l in lines if l[0] == 'M' and l[1] == 'OK']
        self.assertGreaterEqual(len(first), 20)
        last = os.path.join(d, first[-1].lstrip('/'))     # the last file the source had read: cut off on the target
        if os.path.exists(last):
            with open(last, 'r+b') as f:
                f.truncate(os.path.getsize(last) // 2)
        self.assertEqual(int(self.driver('donecount', 'local', d).stdout), max(0, len(first) - 50))
        st = self.status('run', 'local', d)
        self.assertEqual(st['state'], 'done')
        self.assertEqual(st['verify']['bad_n'], 0)
        self.check_content(d)

    def test_an_older_manifest_without_run_markers(self):
        d = self.dest('legacy')
        self.status('run', 'local', d)
        folder = self.job_folder(d)
        mf = os.path.join(folder, 'manifest.tsv')
        with open(mf) as f:
            m = [l for l in f if l.startswith('M\t')]
        with open(mf, 'w') as f:                          # as written by older versions: no R lines
            f.writelines(m)
        self.assertEqual(int(self.driver('donecount', 'local', d).stdout), len(m))        # status.json says done
        sj = os.path.join(folder, 'status.json')
        with open(sj) as f:
            s = json.load(f)
        s['state'] = 'stopped'
        with open(sj, 'w') as f:
            json.dump(s, f)
        self.assertEqual(int(self.driver('donecount', 'local', d).stdout), max(0, len(m) - 50))

    def test_ssh_to_a_linux_target(self):
        """The sha256sum branch (fake ssh runs it here): verify finds a damaged file."""
        d = self.dest('sshlinux')
        os.makedirs(d)
        self.status('run', 'ssh', f'fakehost:{d}')
        with open(os.path.join(d, 'bin/random-0.bin'), 'r+b') as f:
            f.seek(1000)
            f.write(b'\xff' * 16)
        v = self.status('verify', 'ssh', f'fakehost:{d}')['verify']
        self.assertEqual((v['bad_n'], v['missing_n']), (1, 0))
        self.assertIn('/bin/random-0.bin', v['bad_sample'][0])
        self.assertEqual(self.status('recopy', 'ssh', f'fakehost:{d}')['verify']['bad_n'], 0)


if __name__ == '__main__':
    unittest.main()
