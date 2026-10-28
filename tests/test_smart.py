"""Failing disks: doctor reads SMART, the launcher asks before it scans a failing disk."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

import fx  # noqa: I001 - first: puts scripts/ on the path
from test_launcher import LAUNCHER, run

DOCTOR = os.path.join(fx.SCRIPTS, 'doctor.py')
FAKE_SMARTCTL = os.path.join(fx.TESTS, 'bin', 'smartctl-fake')


def doctor_smart(mode, disks=None, smartctl=FAKE_SMARTCTL):
    env = {k: v for k, v in os.environ.items() if not k.startswith('ZFS_SALVAGE_')}
    env.update(FAKE_SMART=mode, ZFS_SALVAGE_SMARTCTL=smartctl, 
               ZFS_SALVAGE_DOCTOR_FAKE=json.dumps({'lsblk': '/dev/fake1 zfs_member', 'disks': {'/dev/fake1': disks or ['/dev/fake']}}))
    state = tempfile.mkdtemp()
    try:
        env['ZFS_SALVAGE_STATE_DIR'] = env['ZFS_SALVAGE_CASES_DIR'] = state          # no case: the repository's cases are not ours
        r = subprocess.run([sys.executable, '-I', DOCTOR, '--json', '--no-root'], capture_output=True, text=True, env=env, timeout=60)
        return next(c for c in json.loads(r.stdout) if c['check'] == 'smart')
    finally:
        fx.rmtree(state)


class SmartInDoctor(unittest.TestCase):
    def test_healthy(self):
        c = doctor_smart('healthy')
        self.assertEqual(c['status'], 'OK')
        self.assertIn('/dev/fake', c['text'])

    def test_every_kind_of_trouble_warns_and_names_the_way_out(self):
        for mode, word in (('pending', '8 sectors waiting'), ('reallocated', '120 reallocated'), ('failed', 'health check failed'),
                           ('nvme', '3 media errors')):
            with self.subTest(mode):
                c = doctor_smart(mode)
                self.assertEqual(c['status'], 'WARN')
                self.assertIn('This disk is failing', c['text'])
                self.assertIn(word, c['text'])
                self.assertIn('ddrescue', c['hint'])

    def test_no_smartctl_is_a_warning_not_silence(self):
        c = doctor_smart('healthy', smartctl='/nonexistent/smartctl')
        self.assertEqual(c['status'], 'WARN')
        self.assertIn('smartmontools', c['hint'])

    def test_unreadable_answer_is_a_warning(self):
        self.assertEqual(doctor_smart('garbage')['status'], 'WARN')

    def test_image_files_have_no_disk_to_ask(self):
        with tempfile.NamedTemporaryFile(suffix='.img') as img, tempfile.TemporaryDirectory() as empty:
            env = dict(os.environ, ZFS_SALVAGE_CASES_DIR=empty, ZFS_SALVAGE_SMARTCTL='/nonexistent/smartctl',
                       ZFS_SALVAGE_DOCTOR_FAKE=json.dumps({'lsblk': f'{img.name} zfs_member'}))
            r = subprocess.run([sys.executable, '-I', DOCTOR, '--json', '--no-root'], capture_output=True, text=True, env=env, timeout=60)
            c = next(c for c in json.loads(r.stdout) if c['check'] == 'smart')
            self.assertEqual(c['status'], 'OK')


@unittest.skipUnless(fx.have_images('modern-single') and fx.ensure_built(), 'needs gcc and the modern-single images')
class ScanOfAFailingDisk(unittest.TestCase):
    def scan(self, smart, anyway=False):
        state, work = tempfile.mkdtemp(), fx.tmpdir()
        try:
            dev = fx.unpack('modern-single', work)[0]
            os.chmod(dev, 0o444)
            env = {'ZFS_SALVAGE_STATE_DIR': state, 'FAKE_SMART': smart,
                   'ZFS_SALVAGE_SMARTCTL': FAKE_SMARTCTL, 'ZFS_SALVAGE_DOCTOR_FAKE': json.dumps({'disks': {dev: ['/dev/fake']}})}
            if anyway:
                env['ZFS_SALVAGE_SCAN_ANYWAY'] = '1'
            r = run('scan', dev, '64', env=env)
            return r, os.path.isdir(os.path.join(state, 'logs', 'raw'))
        finally:
            fx.rmtree(state)
            fx.rmtree(work)

    def test_failing_disk_is_not_read_without_a_yes(self):
        r, scanned = self.scan('pending')
        self.assertEqual(r.returncode, 1)
        self.assertIn('This disk is failing', r.stdout)
        self.assertIn('ZFS_SALVAGE_SCAN_ANYWAY=1', r.stdout)
        self.assertFalse(scanned, 'nothing may be read before the user said yes')

    def test_scan_anyway_goes_ahead(self):
        r, scanned = self.scan('pending', anyway=True)
        self.assertEqual(r.returncode, 0, r.stdout[-300:])
        self.assertIn('ALL CHUNKS: 1/1', r.stdout)

    def test_healthy_disk_is_scanned_without_questions(self):
        r, _ = self.scan('healthy')
        self.assertEqual(r.returncode, 0, r.stdout[-300:])
        self.assertNotIn('failing', r.stdout)


if __name__ == '__main__':
    unittest.main()
