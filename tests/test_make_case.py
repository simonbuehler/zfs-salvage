"""make-case.py: refuses what is not supported and writes valid case files.

Two layers: check_compat() on real `zdb -l` output captured from the fixtures (fast, no docker), and
make-case.py itself run in a container with zdb 2.2.2 (the version on the Ubuntu 24.04 live image) on
the fixture images. The container tests skip without docker.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import unittest

import fx  # noqa: I001 - first: puts scripts/ on the path

DATA = os.path.join(fx.TESTS, 'data', 'zdb-l')
IMAGE = 'zfs-salvage-zdb'

_spec = importlib.util.spec_from_file_location('make_case', os.path.join(fx.SCRIPTS, 'make-case.py'))
mc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mc)
_spec = importlib.util.spec_from_file_location('validate_case', os.path.join(fx.TESTS, 'validate-case.py'))
vc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vc)


def labels(*names):
    out, orig = [], mc.sh
    try:
        for n in names:
            with open(os.path.join(DATA, n + '.txt')) as f:
                text = f.read()
            mc.sh = lambda cmd, timeout=None, _t=text: _t
            out.append(mc.read_label('zdb', f'/dev/{n}'))
    finally:
        mc.sh = orig
    return out


def ds(name, **kw):
    return {'name': name, 'type': 'ZPL', 'readable': True, 'compression': 'lz4', 'checksum': 'fletcher4',
            'encrypted': False, **kw}


def compat(label_names, target='t/home', datasets=None, prof=None, features=None):
    ls = labels(*label_names)
    lab = dict(ls[0])
    lab['active_features'] = features or {}
    datasets = [ds(target)] if datasets is None else datasets
    return mc.check_compat(lab, ls, datasets, target, prof or {})


def kinds(items):
    return [i[0] for i in items]


class CheckCompat(unittest.TestCase):
    def test_plain_pools_pass(self):
        for names in (['legacy-single-d0'], ['modern-mirror-d0', 'modern-mirror-d1'],
                      ['modern-stripe-d0', 'modern-stripe-d1'], ['unsupported-special-d0', 'unsupported-special-d1']):
            with self.subTest(names):
                self.assertEqual(compat(names), ([], []))

    def test_raidz_is_refused(self):
        problems, _ = compat(['raidz1-4-d0', 'raidz1-4-d1', 'raidz1-4-d2', 'raidz1-4-d3'])
        self.assertEqual(kinds(problems), ['layout'])
        self.assertIn('raidz', problems[0][2])

    def test_missing_top_level_vdev_is_refused(self):
        problems, _ = compat(['unsupported-special-d0'], datasets=[])
        self.assertEqual(kinds(problems), ['devices'])
        self.assertIn('[1] of 2', problems[0][2])
        self.assertIn('special', problems[0][3])

    def test_missing_vdev_with_known_allocation_classes(self):
        problems, _ = compat(['unsupported-special-d0'], datasets=[], features={'org.zfsonlinux:allocation_classes': 1})
        self.assertIn('probably a "special" vdev', problems[0][2])

    def test_mirror_with_one_member_is_a_warning(self):
        problems, warnings = compat(['modern-mirror-d0'])
        self.assertEqual(problems, [])
        self.assertEqual(kinds(warnings), ['devices'])

    def test_encrypted_target_is_refused_other_encrypted_dataset_is_not(self):
        problems, _ = compat(['modern-single-d0'] if False else ['legacy-single-d0'], datasets=[ds('t/home', encrypted=True)])
        self.assertEqual(kinds(problems), ['feature'])
        problems, warnings = compat(['legacy-single-d0'], datasets=[ds('t/home'), ds('t/other', encrypted=True)])
        self.assertEqual(problems, [])
        self.assertEqual(kinds(warnings), ['feature'])

    def test_large_dnodes_only_warn(self):
        problems, warnings = compat(['legacy-single-d0'], datasets=[ds('t/home', large_dnodes=True)])
        self.assertEqual(problems, [])
        self.assertIn('large dnodes', warnings[0][2])

    def test_unsupported_data_compression_and_checksum_of_the_target(self):
        problems, _ = compat(['legacy-single-d0'], datasets=[ds('t/home', compression='zstd-3')])
        self.assertEqual(kinds(problems), ['compression'])
        problems, _ = compat(['legacy-single-d0'], datasets=[ds('t/home', checksum='blake3')])
        self.assertEqual(kinds(problems), ['checksum'])
        for c in ('off', 'lzjb', 'lz4', 'on'):
            self.assertEqual(compat(['legacy-single-d0'], datasets=[ds('t/home', compression=c)])[0], [])

    def test_unreadable_target_is_only_a_note(self):
        problems, warnings = compat(['legacy-single-d0'], datasets=[{'name': 't/home', 'readable': False}])
        self.assertEqual(problems, [])
        self.assertEqual(kinds(warnings), ['target'])


class Diagnosis(unittest.TestCase):
    """make-case says where the way to the target's files is broken, from what it read (and the status page draws that)."""
    UBS = [(10, 1), (12, 2)]

    def test_the_four_situations(self):
        ok = {'name': 'p/h', 'readable': True, 'objset_birth': 9}
        bad = {'name': 'p/h', 'readable': False, 'error': 'no valid copy'}
        cases = [('intact', mc.diagnose(self.UBS, 12, [ok], 'p/h')),
                 ('objset', mc.diagnose(self.UBS, 12, [bad], 'p/h')),
                 ('missing', mc.diagnose(self.UBS, 12, [ok], 'p/gone')),
                 ('mos', mc.diagnose(self.UBS, None, [], 'p/h'))]
        for want, d in cases:
            self.assertEqual(d['situation'], want)
            self.assertTrue(d['text'])
        self.assertEqual(cases[1][1]['objset_error'], 'no valid copy')
        self.assertEqual((cases[0][1]['uberblock_txg'], cases[0][1]['mos_txg'], cases[3][1]['mos_txg']), (12, 12, None))
        self.assertIn('older state', cases[0][1]['text'])
        self.assertIn('destroyed', cases[2][1]['text'])


class Questions(unittest.TestCase):
    def test_no_terminal_takes_the_proposed_answer(self):
        """Found by the new-user README walkthrough: make-case crashed with EOFError when started without a terminal."""
        import io
        old = sys.stdin
        sys.stdin = io.StringIO('')
        try:
            self.assertEqual(mc.ask('Dataset', 'p/home', False), 'p/home')
        finally:
            sys.stdin = old
        self.assertEqual(mc.ask('Dataset', 'p/home', True), 'p/home')


class BlockPointerText(unittest.TestCase):
    """zsread.bp_from_text: the root block pointers come from `zdb -luuuu` as text."""
    TEXT = ('DVA[0]=<0:3083000:1000> DVA[1]=<0:131e000:1000> [L0 DMU objset] fletcher4 uncompressed {} LE contiguous '
            'unique double size=1000L/1000P birth=44L/45P fill=74 cksum=000000000000000a:b:c:d')

    def bp(self, text):
        import zsread
        return zsread.BP(zsread.bp_from_text(text))

    def test_fields(self):
        bp = self.bp(self.TEXT.format('unencrypted'))
        self.assertEqual(bp.dvas, [(0, 0x3083000, 0x1000), (0, 0x131e000, 0x1000)])
        self.assertEqual((bp.lsize, bp.psize, bp.comp, bp.cksum_alg, bp.type, bp.level), (0x1000, 0x1000, 2, 7, 11, 0))
        self.assertEqual((bp.birth, bp.fill, bp.cksum, bp.gang, bp.embedded), (44, 74, (10, 11, 12, 13), False, False))

    def test_encryption_flag_and_compression_names(self):
        self.assertFalse(self.bp(self.TEXT.format('unencrypted')).w[6] >> 61 & 1)
        for word in ('authenticated', 'encrypted'):
            self.assertTrue(self.bp(self.TEXT.format(word)).w[6] >> 61 & 1, word)
        self.assertEqual(self.bp(self.TEXT.replace('uncompressed', 'lz4').format('unencrypted')).comp, 15)
        self.assertEqual(self.bp(self.TEXT.replace('uncompressed', 'gzip-6').format('unencrypted')).comp, 10)

    def test_second_vdev_and_gang(self):
        bp = self.bp(self.TEXT.replace('DVA[1]=<0:131e000:1000>', 'DVA[1]=<1:2000:200G>').format('unencrypted'))
        self.assertEqual(bp.dvas[1], (1, 0x2000, 0x200))
        self.assertTrue(bp.gang)

    def test_garbage_is_refused(self):
        import zsread
        for bad in ('', 'rootbp = nothing', self.TEXT.replace('fletcher4', 'rot13').format('unencrypted')):
            with self.assertRaises(ValueError):
                zsread.bp_from_text(bad)

    def test_nicenum_like_zdb(self):
        import zsread
        self.assertEqual([zsread.nicenum(n) for n in (96 * 1024, 304 * 1024, 5965000, 300 * 1024)], ['96K', '304K', '5.69M', '300K'])


class ValidateCase(unittest.TestCase):
    def test_image_files_are_read_only_by_their_write_bit(self):
        """Found by walking through the README with an image file: blockdev --getro fails on files."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'disk0.img')
            with open(path, 'wb') as f:
                f.write(b'x')
            os.chmod(path, 0o644)
            self.assertEqual(mc.read_only_flag(path), '0')
            os.chmod(path, 0o444)
            self.assertEqual(mc.read_only_flag(path), '1')

    def test_repository_cases_are_valid(self):
        for f in ('example.json', 'rpool-2014.json'):
            path = os.path.join(fx.REPO, 'cases', f)
            if os.path.exists(path):
                with open(path) as fh:
                    self.assertEqual(vc.validate(json.load(fh)), [], f)

    def test_detects_problems(self):
        with open(os.path.join(fx.REPO, 'cases', 'example.json')) as f:
            case = json.load(f)
        bad = json.loads(json.dumps(case))
        del bad['target']['dataset']
        bad['pool']['ashift'] = '12'
        bad['timeline'] = []
        got = vc.validate(bad)
        self.assertIn('missing: target.dataset', got)
        self.assertTrue(any(g.startswith('pool.ashift') for g in got))
        self.assertIn('timeline: empty', got)
        self.assertEqual([g for g in vc.validate(bad, partial=True) if g.startswith('timeline')], [])
        forced = dict(case, unsupported=['raidz'])
        self.assertTrue(any('unsupported' in g for g in vc.validate(forced)))


def docker_ready():
    if not shutil.which('docker'):
        return False
    if subprocess.run(['docker', 'image', 'inspect', IMAGE], capture_output=True).returncode == 0:
        return True
    return subprocess.run(['docker', 'build', '-q', '-t', IMAGE, os.path.join(fx.TESTS, 'zdb')],
                          capture_output=True).returncode == 0


@unittest.skipUnless(docker_ready(), 'needs docker (image with zdb 2.2.2, built from tests/zdb)')
class MakeCaseInContainer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = fx.tmpdir()
        cls.out = os.path.join(cls.dir, 'out')
        os.makedirs(cls.out)
        cls.images = {}

    @classmethod
    def tearDownClass(cls):
        fx.rmtree(cls.dir)

    def run_make_case(self, variant, devices, *extra):
        if not fx.have_images(variant):
            self.skipTest(f'no {variant} images')
        if variant not in self.images:
            self.images[variant] = fx.unpack(variant, os.path.join(self.dir, 'img', variant))
        devs = [f'/img/{variant}/{d}' for d in devices]
        name = f'{variant}-{len(devices)}' + ('-forced' if '--force' in extra else '')
        cmd = ['docker', 'run', '--rm', '-v', f'{os.path.join(self.dir, "img")}:/img:ro', '-v', f'{self.out}:/out',
               '-v', f'{fx.SCRIPTS}:/s:ro', IMAGE, 'python3', '/s/make-case.py', '--zdb', 'zdb', '--yes',
               '--target', f'fx-{variant}/home', '--root-names', 'docs,photos', '--name', name, '--out', '/out',
               *sum([['--dev', d] for d in devs], []), *extra]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        path = os.path.join(self.out, name + '.json')
        return r.returncode, r.stdout, path

    def test_supported_pools_give_valid_cases(self):
        for variant, devs in (('legacy-single', ['d0.img']), ('modern-single', ['d0.img']),
                              ('modern-mirror', ['d0.img', 'd1.img']), ('modern-stripe', ['d0.img', 'd1.img']),
                              ('modern-sha256', ['d0.img']), ('modern-twodatasets', ['d0.img'])):
            with self.subTest(variant):
                rc, out, path = self.run_make_case(variant, devs)
                self.assertEqual(rc, 0, out[-600:])
                with open(path) as f:
                    case = json.load(f)
                self.assertEqual(vc.validate(case), [], variant)
                self.assertEqual(case['pool']['name'], f'fx-{variant}')
                self.assertEqual(case['profile']['meta_dnode_levels'], 6)
                self.assertEqual(len(case['pool']['vdevs']), len(case['pool']['devices']) if variant == 'modern-stripe' else 1)

    def test_diagnosis_is_written_and_matches_the_pool(self):
        """undamaged → intact; destroyed objset block → objset; a destroyed dataset → missing (the name is typed in)."""
        for variant, devs, target, want in (('modern-single', ['d0.img'], None, 'intact'),
                                            ('modern-destroyed', ['d0.img'], 'fx-modern-destroyed/home', 'missing')):
            with self.subTest(variant):
                rc, out, path = self.run_make_case(variant, devs, *(['--target', target] if target else []))
                self.assertEqual(rc, 0, out[-600:])
                self.assertIn('Diagnosis:', out)
                with open(path) as f:
                    case = json.load(f)
                self.assertEqual(case['diagnosis']['situation'], want)

    def test_damaged_pool_is_diagnosed_as_a_broken_objset_block(self):
        if not fx.have_images('modern-single'):
            self.skipTest('no modern-single images')
        dest = os.path.join(self.dir, 'img', 'modern-single-damaged')
        fx.unpack('modern-single', dest)
        fx.damage(dest, 'modern-single')
        r = subprocess.run(['docker', 'run', '--rm', '-v', f'{os.path.join(self.dir, "img")}:/img:ro', '-v', f'{self.out}:/out',
                            '-v', f'{fx.SCRIPTS}:/s:ro', IMAGE, 'python3', '/s/make-case.py', '--yes', '--name', 'diag-objset',
                            '--out', '/out', '--dev', '/img/modern-single-damaged/d0.img'], capture_output=True, text=True, timeout=600)
        self.assertEqual(r.returncode, 0, r.stdout[-600:])
        with open(os.path.join(self.out, 'diag-objset.json')) as f:
            case = json.load(f)
        self.assertEqual(case['diagnosis']['situation'], 'objset')
        self.assertEqual(case['target']['dataset'], 'fx-modern-single/home', 'the only unreadable dataset is proposed')
        self.assertIn('objset block cannot be read', r.stdout)

    def test_several_unreadable_datasets_are_not_guessed(self):
        """Needs two broken datasets: fake the dataset list instead of building such a pool."""
        import io
        import contextlib
        datasets = [{'name': 'p/a', 'readable': False, 'error': 'x'}, {'name': 'p/b', 'readable': False, 'error': 'x'},
                    {'name': 'p', 'readable': True, 'type': 'ZPL', 'meta_dnode': {'nlevels': 6, 'indirect_block_size': 131072, 'dnode_block_size': 16384}}]
        labels = {'device': '/dev/x', 'pool': 'p', 'pool_guid': 1, 'txg': 5, 'guid': 2, 'top_guid': 3, 'ashift': 12, 'version': 5000,
                  'vdev_children': 1, 'top_type': 'disk', 'top_id': 0, 'children': 0, 'vdev_types': ['disk'], 'children_paths': [],
                  'features_for_read': []}
        saved = (mc.read_label, mc.read_uberblocks, mc.zsread_datasets, sys.argv)
        mc.read_label = lambda zdb, d: dict(labels, device=d)
        mc.read_uberblocks = lambda zdb, devs: ([(5, 1)], {5: 'DVA[0]=<0:1:1000>'})
        mc.zsread_datasets = lambda *a, **k: {'rootbp_index': 0, 'datasets': datasets, 'features_for_write': {}, 'errors': []}
        out = io.StringIO()
        try:
            sys.argv = ['make-case', '--dev', '/dev/null', '--yes', '--out', '/nonexistent']
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
                mc.main()
        finally:
            mc.read_label, mc.read_uberblocks, mc.zsread_datasets, sys.argv = saved
        self.assertIn('--target', str(cm.exception.code))
        self.assertIn('2 datasets cannot be read', out.getvalue())

    def test_refusals(self):
        """Each unsupported fixture: exit code 2, no case file, the reason in plain words and the alternative."""
        for variant, devs, needles in (
                ('raidz1-4', ['d0.img', 'd1.img', 'd2.img', 'd3.img'],
                 ['is raidz, which zfs-salvage cannot read yet', 'PhotoRec']),
                ('raidz1-4', ['d0.img', 'd1.img', 'd2.img'],            # = raidz1-4-missing: one disk less
                 ['is raidz, which zfs-salvage cannot read yet', 'has only 3 of 4 disks here', 'rebuilding a missing disk from parity']),
                ('unsupported-encrypted', ['d0.img'],
                 ['natively encrypted: its files cannot be read without the keys', 'zfs load-key']),
                ('unsupported-special', ['d0.img'],
                 ['top-level vdev(s) [1] of 2 have no device', '"special" vdev it held the metadata', 'PhotoRec'])):
            with self.subTest(variant, devices=len(devs)):
                rc, out, path = self.run_make_case(variant, devs)
                self.assertEqual(rc, 2, out[-600:])
                self.assertIn('NOT SUPPORTED', out)
                for needle in needles:
                    self.assertIn(needle, out)
                self.assertFalse(os.path.exists(path), 'no case file may be written')

    def test_large_dnodes_and_missing_mirror_member_warn_but_pass(self):
        rc, out, path = self.run_make_case('unsupported-largednode', ['d0.img'])
        self.assertEqual(rc, 0, out[-600:])
        self.assertIn('large dnodes', out)
        rc, out, path = self.run_make_case('modern-mirror', ['d0.img'])
        self.assertEqual(rc, 0, out[-600:])
        self.assertIn('only 1 of 2 members', out)

    def test_force_writes_the_case_and_marks_it(self):
        rc, out, path = self.run_make_case('raidz1-4', ['d0.img', 'd1.img', 'd2.img', 'd3.img'], '--force')
        self.assertEqual(rc, 0, out[-600:])
        with open(path) as f:
            case = json.load(f)
        self.assertTrue(case['unsupported'])
        self.assertTrue(any(c['blocking'] for c in case['compatibility']))
        self.assertTrue(any('unsupported' in g for g in vc.validate(case)))


if __name__ == '__main__':
    unittest.main()
