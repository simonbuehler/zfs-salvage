"""Where the rescued files may go: not into RAM, not onto a read-only mount, not onto a disk that holds a pool
member (the loop device case included); a failing target disk is a warning. System facts are faked
(ZFS_SALVAGE_DOCTOR_FAKE), the checks run unchanged."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

import fx  # noqa: I001 - first: puts scripts/ on the path

SMARTCTL = os.path.join(fx.TESTS, 'bin', 'smartctl-fake')


class Target(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.dest = os.path.join(self.dir, 'rescued')          # does not exist yet: the nearest parent is judged
        os.makedirs(os.path.join(self.dir, 'cases'))

    def tearDown(self):
        fx.rmtree(self.dir)

    def call(self, fake, pool=('/dev/fake1',), smart='healthy', fn='check'):
        with open(os.path.join(self.dir, 'case.json'), 'w') as f:
            json.dump({'pool': {'devices': [{'path': p, 'vdev': 0} for p in pool]}, 'target': {'dataset': 'p/x'}}, f)
        env = {k: v for k, v in os.environ.items() if k != 'ZFS_SALVAGE_ALLOW_RAM_TARGET'}
        env.update(ZFS_SALVAGE_CASE=os.path.join(self.dir, 'case.json'), ZFS_SALVAGE_STATE_DIR=self.dir,
                   ZFS_SALVAGE_CATALOG_DIR=os.path.join(self.dir, 'catalog'), 
                   ZFS_SALVAGE_SMARTCTL=SMARTCTL, FAKE_SMART=smart, ZFS_SALVAGE_DOCTOR_FAKE=json.dumps(fake))
        code = ("import json, sys; sys.path.insert(0, sys.argv[1]); import transfer; "
                f"print(json.dumps(transfer.{fn}({{'type': 'local', 'dest': sys.argv[2]}})))")
        r = subprocess.run([sys.executable, '-I', '-c', code, fx.SCRIPTS, self.dest], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        return json.loads(r.stdout)

    def fake(self, fs='ext4', source='/dev/fake2', opts='rw', disks=None, **more):
        return dict(mounts={self.dir: ['/', fs]}, sources={self.dir: source}, mount_options={self.dir: opts},
                    disks=disks if disks is not None else {'/dev/fake1': ['/dev/diskA'], '/dev/fake2': ['/dev/diskB']}, **more)

    def test_another_disk_passes(self):
        r = self.call(self.fake())
        self.assertTrue(r['ok'], r)
        self.assertNotIn('warning', r)

    def test_ram_is_refused(self):
        for fs in ('tmpfs', 'overlay'):
            with self.subTest(fs):
                r = self.call(self.fake(fs=fs))
                self.assertFalse(r['ok'])
                self.assertIn(f'in RAM ({fs})', r['info'])

    def test_read_only_mount_is_refused(self):
        r = self.call(self.fake(opts='ro,relatime'))
        self.assertFalse(r['ok'])
        self.assertIn('read-only', r['info'])

    def test_a_disk_that_holds_a_pool_member_is_refused(self):
        r = self.call(self.fake(source='/dev/fake3', disks={'/dev/fake1': ['/dev/diskA'], '/dev/fake3': ['/dev/diskA']}))
        self.assertFalse(r['ok'])
        self.assertIn('/dev/diskA also holds the pool (/dev/fake1)', r['info'])
        self.assertIn('choose another disk', r['info'])

    def test_a_pool_on_a_loop_device_counts_as_the_disk_of_its_backing_file(self):
        """A fixture attached as a loop device: its file lives on a file system on diskA, and so does the target."""
        fake = dict(self.fake(source='/dev/fake3', disks={'/dev/fake3': ['/dev/diskA'], '/dev/fake9': ['/dev/diskA']}),
                    loop_backing={'/dev/loop7': '/images/pool.img'})
        fake['mounts']['/images/pool.img'] = ['/images', 'ext4']
        fake['sources']['/images/pool.img'] = '/dev/fake9'
        r = self.call(fake, pool=('/dev/loop7',))
        self.assertFalse(r['ok'], r)
        self.assertIn('/dev/diskA also holds the pool (/dev/loop7)', r['info'])
        # and a target on another disk is fine
        fake['sources'][self.dir] = '/dev/fake4'
        fake['disks']['/dev/fake4'] = ['/dev/diskC']
        self.assertTrue(self.call(fake, pool=('/dev/loop7',))['ok'])

    def test_image_files_have_no_disk_and_do_not_block(self):
        self.assertTrue(self.call(self.fake(), pool=('/somewhere/pool.img',))['ok'])

    def test_failing_target_disk_is_a_warning_not_a_refusal(self):
        r = self.call(self.fake(), smart='pending')
        self.assertTrue(r['ok'])
        self.assertIn('this disk is failing', r['warning'])
        self.assertIn('/dev/diskB', r['warning'])

    def test_start_refuses_what_check_refuses(self):
        r = self.call(self.fake(fs='tmpfs'), fn='start')
        self.assertEqual(r['state'], 'refused')
        self.assertIn('in RAM', r['info'])
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'catalog', 'transfers')), 'no job may be created')


if __name__ == '__main__':
    unittest.main()
