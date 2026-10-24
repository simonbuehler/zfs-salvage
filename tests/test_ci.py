"""The CI workflow only uses what exists, and the fixture asset round trip works."""
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import unittest

try:
    import yaml
except ImportError:        # the workflow test needs PyYAML, everything else does not
    yaml = None

import fx  # noqa: I001 - first: puts scripts/ on the path

WORKFLOW = os.path.join(fx.REPO, '.github', 'workflows', 'ci.yml')


def workflow():
    with open(WORKFLOW) as f:
        return yaml.safe_load(f)


@unittest.skipUnless(yaml, 'needs PyYAML')
class Workflow(unittest.TestCase):
    def test_shape(self):
        w = workflow()
        job = w['jobs']['test']
        self.assertEqual(job['runs-on'], 'ubuntu-24.04')
        self.assertEqual(w['permissions'], {'contents': 'read'})
        self.assertIn('push', w[True] if True in w else w['on'])          # PyYAML reads the key `on` as True

    def test_every_script_it_runs_exists(self):
        text = '\n'.join(s.get('run', '') for s in workflow()['jobs']['test']['steps'])
        paths = set(re.findall(r'(?:^|\s)((?:tests|tools|scripts)/[\w./-]+\.(?:sh|py))', text))
        self.assertGreaterEqual(len(paths), 5, paths)
        for p in paths:
            self.assertTrue(os.access(os.path.join(fx.REPO, p), os.X_OK), f'{p} is missing or not executable')

    def test_it_runs_the_whole_chain_in_order(self):
        runs = [s.get('run', '') for s in workflow()['jobs']['test']['steps']]
        joined = '\n'.join(runs)
        order = ['tools/build-portable.sh', 'tests/fetch-fixtures.sh', 'unittest discover', 'tests/run.sh --matrix',
                 'git diff --exit-code tests/capabilities.json', 'scripts/gen-matrix.py --check', 'tests/e2e-local.sh']
        positions = [joined.index(x) for x in order]
        self.assertEqual(positions, sorted(positions))

    def test_fixtures_come_from_the_repository_s_own_release(self):
        self.assertIn('${{ github.repository }}', workflow()['env']['FIXTURES_URL'])


class FixtureAsset(unittest.TestCase):
    def test_round_trip_and_a_tampered_asset_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            def tree(name):
                root = os.path.join(d, name, 'tests')
                os.makedirs(os.path.join(root, 'fixtures', 'v1'))
                for s in ('pack-fixtures.sh', 'fetch-fixtures.sh'):
                    shutil.copy(os.path.join(fx.TESTS, s), root)
                return root
            src = tree('src')
            image = os.path.join(src, 'fixtures', 'v1', 'd0.img.xz')
            with open(image, 'wb') as f:
                f.write(b'not really xz' * 100)
            with open(os.path.join(src, 'fixtures', 'v1.json'), 'w') as f:
                f.write('{}')
            with open(os.path.join(src, 'fixtures', 'SHA256SUMS'), 'w') as f:
                f.write(hashlib.sha256(b'not really xz' * 100).hexdigest() + '  v1/d0.img.xz\n')
            asset = os.path.join(d, 'fixtures.tar')
            r = subprocess.run(['bash', os.path.join(src, 'pack-fixtures.sh'), asset], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            names = subprocess.run(['tar', '-tf', asset], capture_output=True, text=True).stdout.split()
            self.assertEqual(names, ['v1/d0.img.xz'], 'only the images go into the asset, not the sidecars that are in git')

            dst = tree('dst')
            shutil.copy(os.path.join(src, 'fixtures', 'SHA256SUMS'), os.path.join(dst, 'fixtures'))
            r = subprocess.run(['bash', os.path.join(dst, 'fetch-fixtures.sh'), asset], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            with open(os.path.join(dst, 'fixtures', 'v1', 'd0.img.xz'), 'rb') as f:
                self.assertEqual(f.read(), b'not really xz' * 100)

            with open(image, 'ab') as f:                                 # the images no longer match the sums
                f.write(b'x')
            r = subprocess.run(['bash', os.path.join(src, 'pack-fixtures.sh'), asset], capture_output=True, text=True)
            self.assertEqual(r.returncode, 1)
            self.assertIn('do not match SHA256SUMS', r.stdout)


if __name__ == '__main__':
    unittest.main()
