"""make-case survives a zdb that is missing, hangs or aborts, and a pool whose metadata does not verify; the
metadata layout is taken from a verified tree instead of being guessed. Only the label calls
(`zdb -l`, `-luuuu`) are made, so a zdb that fails on every other call does not matter any more."""
import gzip
import json
import os
import subprocess
import sys
import time
import unittest

import fx  # noqa: I001 - first: puts scripts/ on the path
import test_make_case as tmc
from test_visible_errors import env_for

BIN = os.path.join(fx.TESTS, 'bin')
validate = tmc.vc.validate


class MissingZdb(unittest.TestCase):
    def test_clear_message_and_no_traceback(self):
        out = fx.tmpdir()
        try:
            r = subprocess.run([sys.executable, '-I', os.path.join(fx.SCRIPTS, 'make-case.py'), '--zdb', '/nonexistent/zdb',
                                '--dev', '/dev/null', '--yes', '--out', out], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 1)
            self.assertNotIn('Traceback', r.stderr)
            self.assertIn('No readable ZFS label', r.stderr)
            self.assertIn('/nonexistent/zdb not found', r.stderr)
            self.assertIn('install zfsutils-linux', r.stderr)
        finally:
            fx.rmtree(out)


@unittest.skipUnless(tmc.docker_ready(), 'needs docker (image with zdb 2.2.2, built from tests/zdb)')
class FlakyZdb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = fx.tmpdir()
        cls.out = os.path.join(cls.dir, 'out')
        os.makedirs(cls.out)

    @classmethod
    def tearDownClass(cls):
        fx.rmtree(cls.dir)

    def run_flaky(self, mode, variant='modern-single', zdb='zdb-flaky'):
        if not fx.have_images(variant):
            self.skipTest(f'no {variant} images')
        fx.unpack(variant, os.path.join(self.dir, 'img', variant))
        name = f'{variant}-{mode}'
        t0 = time.time()
        r = subprocess.run(['docker', 'run', '--rm', '-e', f'FLAKY={mode}', '-v', f'{os.path.join(self.dir, "img")}:/img:ro',
                            '-v', f'{self.out}:/out', '-v', f'{fx.SCRIPTS}:/s:ro', '-v', f'{BIN}:/b:ro', tmc.IMAGE,
                            'python3', '/s/make-case.py', '--zdb', f'/b/{zdb}', '--zdb-timeout', '5', '--zdb-budget', '12',
                            '--yes', '--target', f'fx-{variant}/home', '--root-names', 'docs', '--name', name,
                            '--out', '/out', '--dev', f'/img/{variant}/d0.img'],
                           capture_output=True, text=True, timeout=300)
        with open(os.path.join(self.out, name + '.json')) as f:
            case = json.load(f)
        return r, case, time.time() - t0

    def test_zdb_that_hangs_or_aborts_on_other_calls_does_not_matter(self):
        for mode in ('hang', 'abort'):
            with self.subTest(mode):
                r, case, secs = self.run_flaky(mode)
                self.assertEqual(r.returncode, 0, r.stdout[-800:])
                self.assertEqual(validate(case), [], 'a complete case, as if zdb worked')
                self.assertEqual(case['problems'], [])
                self.assertEqual(case['profile']['meta_dnode_levels'], 6)
                self.assertLess(secs, 60)

    def test_metadata_that_does_not_verify_gives_a_partial_case(self):
        r, case, secs = self.run_flaky('abort', zdb='zdb-nomos')
        self.assertEqual(r.returncode, 0, r.stdout[-800:])
        self.assertIn("no uberblock's metadata is readable", r.stdout)
        self.assertEqual(validate(case, partial=True), [])
        self.assertEqual(case['pool']['name'], 'fx-modern-single')              # from the label
        self.assertTrue(case['timeline'], 'uberblock timeline from zdb -luuuu')
        self.assertEqual(case['datasets'], [])
        self.assertEqual(case['diagnosis']['situation'], 'mos')
        self.assertIn('Diagnosis:', r.stdout)
        self.assertIn('profile.meta_dnode_levels', ' '.join(validate(case)).replace('missing: ', ''))


@unittest.skipUnless(fx.have_images('modern-single') and fx.ensure_built(), 'needs gcc and the modern-single images')
class ProfileFromTree(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import test_visible_errors as tve
        cls.dir = fx.tmpdir()
        cls.dev = fx.unpack('modern-single', os.path.join(cls.dir, 'img'))[0]
        cls.side = fx.sidecars()['modern-single']
        cls.raw = os.path.join(cls.dir, 'raw')
        os.makedirs(cls.raw)
        scan = subprocess.run([fx.ZSCAN, cls.dev], capture_output=True, text=True).stdout
        with gzip.open(os.path.join(cls.raw, 'chunk_0000.tsv.gz'), 'wt') as f:
            f.write(scan)
        open(os.path.join(cls.raw, 'chunk_0000.tsv.gz.done'), 'w').close()
        cls.tve = tve

    @classmethod
    def tearDownClass(cls):
        fx.rmtree(cls.dir)

    def test_levels_are_inferred_from_the_objset_blocks(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('rt', os.path.join(fx.SCRIPTS, 'rank-trees.py'))
        rt = importlib.util.module_from_spec(spec)
        os.environ.setdefault('ZFS_SALVAGE_CASE', os.path.join(fx.REPO, 'cases', 'example.json'))
        spec.loader.exec_module(rt)
        self.assertEqual(rt.infer_levels(self.raw), 6)
        self.assertEqual(rt.candidates(self.raw, None, 4 << 20), rt.candidates(self.raw, 6, 4 << 20))
        self.assertTrue(rt.candidates(self.raw, None, 4 << 20))

    def test_rank_trees_measures_the_profile_when_the_case_has_none(self):
        """A case from a pool that did not open has no meta-dnode profile; the first verified tree gives it."""
        state = os.path.join(self.dir, 'state')
        os.makedirs(os.path.join(state, 'status'))
        names = sorted({f['path'].split('/')[1] for f in self.side['files']})
        case = {'name': 't', 'pool': {'name': 'p', 'devices': [{'path': self.dev, 'vdev': 0}]},
                'profile': {'label_start': self.side['label_start'], 'compression': [], 'checksum': []},
                'timeline': [{'txg': 100, 'time': 1}], 'target': {'dataset': 'p/home', 'root_names': names},
                'scan': {'device': 'd0', 'chunk_dir': self.raw, 'log': 'x.log'}}
        cpath = os.path.join(self.dir, 'case.json')
        with open(cpath, 'w') as f:
            json.dump(case, f)
        zdir = fx.tmpdir()
        import shutil
        shutil.copy(os.path.join(fx.SCRIPTS, 'zsread.py'), zdir)
        shutil.copy(fx.LIB, zdir)
        e = env_for(state, 'local', ZFS_SALVAGE_CASE=cpath, ZFS_SALVAGE_READER=os.path.join(zdir, 'zsread.py'))
        r = subprocess.run([sys.executable, '-I', os.path.join(fx.SCRIPTS, 'rank-trees.py'), '30'], env=e,
                           capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        with open(os.path.join(state, 'status', 'tree_profile.json')) as f:
            prof = json.load(f)
        self.assertEqual((prof['meta_dnode_levels'], prof['indirect_block_size'], prof['metadata_compression']),
                         (6, 131072, 'lz4'))
        with open(os.path.join(state, 'status', 'target_candidates.json')) as f:
            self.assertTrue(json.load(f))
        fx.rmtree(zdir)


if __name__ == '__main__':
    unittest.main()
