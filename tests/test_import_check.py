"""doctor refuses to go on while the pool is imported or set up to be imported."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

import fx  # noqa: I001 - first: puts scripts/ on the path

DOCTOR = os.path.join(fx.SCRIPTS, 'doctor.py')
ZPOOL = os.path.join(fx.TESTS, 'bin', 'zpool-fake')


def check(pools='', cache=None, services=None, names=('tank',), zpool=ZPOOL):
    with tempfile.TemporaryDirectory() as d:
        cache_path = os.path.join(d, 'zpool.cache')
        if cache is not None:
            with open(cache_path, 'wb') as f:
                f.write(cache)
        env = {k: v for k, v in os.environ.items() if not k.startswith('ZFS_SALVAGE_')}
        env.update(ZFS_SALVAGE_STATE_DIR=d, ZFS_SALVAGE_CASES_DIR=d, ZFS_SALVAGE_ZPOOL=zpool,
                   ZFS_SALVAGE_ZPOOL_CACHE=cache_path, FAKE_POOLS=pools,
                   ZFS_SALVAGE_DOCTOR_FAKE=json.dumps({'pool_names': list(names), 'services': services or {}}))
        r = subprocess.run([sys.executable, '-I', DOCTOR, '--json', '--no-root'], capture_output=True, text=True, env=env, timeout=60)
        return next(c for c in json.loads(r.stdout) if c['check'] == 'import'), r.returncode


class ImportCheck(unittest.TestCase):
    def test_imported_pool_is_a_failure_with_the_export_command(self):
        c, rc = check(pools='tank other')
        self.assertEqual(c['status'], 'FAIL')
        self.assertIn('the pool tank is imported on this machine', c['text'])
        self.assertIn('writes to the disks', c['text'])
        self.assertIn('sudo zpool export -f tank', c['hint'])
        self.assertEqual(rc, 1)

    def test_other_pools_do_not_matter(self):
        self.assertEqual(check(pools='rpool')[0]['status'], 'OK')

    def test_pool_in_the_cache_file_is_a_failure(self):
        c, _ = check(cache=b'\x00\x01tank\x00\x02')
        self.assertEqual(c['status'], 'FAIL')
        self.assertIn('is listed in', c['text'])
        self.assertIn('sudo mv', c['hint'])

    def test_cache_file_without_the_pool_is_fine(self):
        self.assertEqual(check(cache=b'rpool')[0]['status'], 'OK')

    def test_import_scan_service_is_a_warning(self):
        c, rc = check(services={'zfs-import-scan.service': 'enabled'})
        self.assertEqual(c['status'], 'WARN')
        self.assertIn('sudo systemctl disable zfs-import-scan.service', c['hint'])
        self.assertEqual(check(services={'zfs-import-scan.service': 'disabled'})[0]['status'], 'OK')

    def test_everything_clean(self):
        c, _ = check()
        self.assertEqual(c['status'], 'OK')
        self.assertIn('tank is not imported', c['text'])

    def test_no_zpool_program_means_nothing_can_be_imported(self):
        self.assertEqual(check(pools='tank', zpool='/nonexistent/zpool')[0]['status'], 'OK')

    def test_no_pool_name_yet(self):
        c, _ = check(names=())
        self.assertEqual(c['status'], 'OK')


if __name__ == '__main__':
    unittest.main()
