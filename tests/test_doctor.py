"""doctor: every problem is one clear line. System facts are faked with ZFS_SALVAGE_DOCTOR_FAKE, tools
and paths with environment variables, so each failure can be produced on any machine."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

import fx  # noqa: I001 - first: puts scripts/ on the path
from test_visible_errors import env_for

DOCTOR = os.path.join(fx.SCRIPTS, 'doctor.py')
ZDB_OK = os.path.join(fx.TESTS, 'bin', 'zdb-ok')


@unittest.skipUnless(fx.ensure_built(), 'needs gcc (zscan, libzsalvage.so)')
class Doctor(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='zfs-salvage-doctor-')
        self.img = os.path.join(self.dir, 'pool.img')
        with open(self.img, 'wb') as f:
            f.truncate(1 << 20)
        os.chmod(self.img, 0o444)                                   # read-only
        with open(os.path.join(fx.REPO, 'cases', 'example.json')) as f:
            self.case = json.load(f)
        self.case['pool']['devices'] = [{'path': self.img, 'vdev': 0}]
        self.write_case(self.case)
        self.fake = {'euid': 0}

    def tearDown(self):
        os.chmod(self.img, 0o644)
        fx.rmtree(self.dir)

    def write_case(self, case):
        os.makedirs(os.path.join(self.dir, 'cases'), exist_ok=True)
        with open(os.path.join(self.dir, 'cases', 'c.json'), 'w') as f:
            json.dump(case, f)

    def doctor(self, args=(), fake=None, mode='local', **env):
        settings = dict(ZFS_SALVAGE_CASES_DIR=os.path.join(self.dir, 'cases'), ZFS_SALVAGE_ZDB=ZDB_OK,
                        ZFS_SALVAGE_HOST='local' if mode == 'local' else 'fakehost',
                        ZFS_SALVAGE_DOCTOR_FAKE=json.dumps(dict(self.fake, **(fake or {}))))
        settings.update(env)                                         # a test may override any of them
        e = env_for(os.path.join(self.dir, 'state'), mode, **settings)
        e.pop('ZFS_SALVAGE_CASE')                                    # find the case in the cases directory
        r = subprocess.run([sys.executable, '-I', DOCTOR, '--json', *args], env=e, capture_output=True, text=True, timeout=120)
        self.assertIn(r.returncode, (0, 1), r.stderr)
        res = {x['check']: x for x in json.loads(r.stdout)}
        return res, r.returncode

    def status(self, check, **kw):
        res, rc = self.doctor(**kw)
        return res[check]['status'], res[check]['text'], rc

    def test_a_correct_setup_is_all_ok(self):
        res, rc = self.doctor()
        self.assertEqual({k: v['status'] for k, v in res.items()}, {k: 'OK' for k in res}, res)
        self.assertEqual(rc, 0)

    def test_not_root(self):
        st, text, rc = self.status('root', fake={'euid': 1000})
        self.assertEqual((st, rc), ('FAIL', 1))
        self.assertIn('sudo', self.doctor(fake={'euid': 1000})[0]['root']['hint'])
        self.assertEqual(self.doctor(args=['--no-root'], fake={'euid': 1000})[0]['root']['status'], 'WARN')

    def test_old_python(self):
        self.assertEqual(self.status('python', fake={'python': [3, 6, 9]})[0], 'FAIL')

    def test_zdb_missing_and_without_label(self):
        st, text, _ = self.status('zdb', ZFS_SALVAGE_ZDB='/nonexistent/zdb')
        self.assertEqual(st, 'FAIL')
        self.assertIn('not installed', text)
        st, text, _ = self.status('zdb', ZDB_MODE='nolabel')
        self.assertEqual(st, 'FAIL')
        self.assertIn('shows no ZFS label', text)

    def test_zscan_missing(self):
        st, text, _ = self.status('zscan', ZFS_SALVAGE_ZSCAN='/nonexistent/zscan')
        self.assertEqual(st, 'FAIL')
        self.assertIn('not built', text)

    def test_library_that_cannot_be_loaded_is_a_warning(self):
        bad = os.path.join(self.dir, 'libzsalvage.so')
        with open(bad, 'wb') as f:
            f.write(b'not an elf file')
        st, text, rc = self.status('libzsalvage', fake={'lib_paths': [bad]})
        self.assertEqual(st, 'WARN')
        self.assertIn('much slower', text)

    def test_data_directory_in_ram_or_full(self):
        st, text, rc = self.status('data directory', fake={'mounts': {'x': ['/', 'tmpfs']}})
        self.assertEqual((st, rc), ('FAIL', 1))
        self.assertIn('in RAM', text)
        st, text, _ = self.status('data directory', fake={'free_bytes': 1000})
        self.assertEqual(st, 'FAIL')
        self.assertIn('at least 1 GB', text)

    def test_devices(self):
        os.chmod(self.img, 0o644)                                    # writable
        st, text, _ = self.status('devices')
        self.assertEqual(st, 'FAIL')
        self.assertIn('not read-only', text)
        os.chmod(self.img, 0o444)
        case = dict(self.case, pool=dict(self.case['pool'], devices=[{'path': '/nonexistent/dev', 'vdev': 0}]))
        self.write_case(case)
        st, text, _ = self.status('devices')
        self.assertEqual(st, 'FAIL')
        self.assertIn('do not exist', text)

    def test_no_case_is_a_warning_and_a_broken_one_a_failure(self):
        os.remove(os.path.join(self.dir, 'cases', 'c.json'))
        self.assertEqual(self.status('case file')[0], 'WARN')
        self.write_case({'name': 'x'})
        st, text, _ = self.status('case file')
        self.assertEqual(st, 'FAIL')
        self.assertIn('not valid', text)

    def test_unsupported_pool(self):
        self.write_case(dict(self.case, unsupported=['top-level vdev 0 is raidz']))
        st, text, _ = self.status('pool features')
        self.assertEqual(st, 'FAIL')
        self.assertIn('raidz', text)
        blocking = dict(self.case, compatibility=[{'what': 'layout', 'text': 'the target is encrypted', 'blocking': True}])
        self.write_case(blocking)
        self.assertIn('encrypted', self.status('pool features')[1])

    def test_little_memory(self):
        self.assertEqual(self.status('memory', fake={'mem_kb': 1000000})[0], 'WARN')

    def test_unreachable_recovery_host(self):
        res, rc = self.doctor(mode='down')
        self.assertEqual(res['recovery host']['status'], 'FAIL')
        self.assertIn('without a password', res['recovery host']['text'])
        self.assertEqual(res['zscan']['status'], 'FAIL')
        self.assertIn('not reachable', res['zscan']['text'])
        self.assertEqual(rc, 1)


class DefaultHost(unittest.TestCase):
    """Found by running doctor from the bundle on a fresh Ubuntu 24.04: without a case the host was `recovery-host`."""

    def host(self, **env):
        with tempfile.TemporaryDirectory() as empty:
            base = {k: v for k, v in os.environ.items() if not k.startswith('ZFS_SALVAGE_')}
            r = subprocess.run([sys.executable, '-I', '-c', 'import sys; sys.path.insert(0, sys.argv[1]); import common; print(common.HOST)',
                                fx.SCRIPTS], capture_output=True, text=True, env=dict(base, ZFS_SALVAGE_CASES_DIR=empty, **env))
        return r.stdout.strip()

    def test_without_a_case_everything_is_local(self):
        self.assertEqual(self.host(), 'local')

    def test_case_with_devices_that_are_not_here_means_remote(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'c.json')
            with open(path, 'w') as f:
                json.dump({'pool': {'devices': [{'path': '/dev/mapper/not-here'}]}}, f)
            self.assertEqual(self.host(ZFS_SALVAGE_CASE=path), 'recovery-host')

    def test_explicit_host_wins(self):
        self.assertEqual(self.host(ZFS_SALVAGE_HOST='me@box'), 'me@box')


if __name__ == '__main__':
    unittest.main()
