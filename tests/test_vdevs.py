"""Several top-level vdevs: vdev ids from the labels, the vdev map of zsread, zscan --max-vdev."""
import importlib.util
import os
import subprocess
import unittest

import fx  # noqa: I001 - first: puts scripts/ on the path
import common
import zsread

DATA = os.path.join(fx.TESTS, 'data', 'zdb-l')       # `zdb -l` of fixture devices (zdb 2.2.2, in Docker)
LABEL_START = 4 << 20

_spec = importlib.util.spec_from_file_location('make_case', os.path.join(fx.SCRIPTS, 'make-case.py'))
make_case = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(make_case)


def labels(*names):
    """Run make-case's label parser on captured `zdb -l` output instead of a device."""
    out, orig = [], make_case.sh
    try:
        for n in names:
            with open(os.path.join(DATA, n + '.txt')) as f:
                text = f.read()
            make_case.sh = lambda cmd, timeout=None, _t=text: _t
            out.append(make_case.read_label('zdb', f'/dev/{n}'))
    finally:
        make_case.sh = orig
    return out


class LabelParsing(unittest.TestCase):
    def test_top_level_ids_and_types(self):
        got = {l['device']: (l['top_id'], l['top_type']) for l in labels(
            'modern-stripe-d0', 'modern-stripe-d1', 'modern-mirror-d0', 'modern-mirror-d1',
            'raidz1-4-d2', 'unsupported-special-d1', 'legacy-single-d0')}
        self.assertEqual(got, {
            '/dev/modern-stripe-d0': (0, 'file'), '/dev/modern-stripe-d1': (1, 'file'),
            '/dev/modern-mirror-d0': (0, 'mirror'), '/dev/modern-mirror-d1': (0, 'mirror'),
            '/dev/raidz1-4-d2': (0, 'raidz'), '/dev/unsupported-special-d1': (1, 'file'),
            '/dev/legacy-single-d0': (0, 'file')})

    def test_vdev_groups(self):
        self.assertEqual(make_case.vdev_groups(labels('modern-stripe-d0', 'modern-stripe-d1')),
                         [{'id': 0, 'type': 'file', 'devices': ['/dev/modern-stripe-d0']},
                          {'id': 1, 'type': 'file', 'devices': ['/dev/modern-stripe-d1']}])
        self.assertEqual(make_case.vdev_groups(labels('modern-mirror-d0', 'modern-mirror-d1')),
                         [{'id': 0, 'type': 'mirror', 'devices': ['/dev/modern-mirror-d0', '/dev/modern-mirror-d1']}])


class ReaderCommand(unittest.TestCase):
    def setUp(self):
        self.saved = common.CASE

    def tearDown(self):
        common.CASE = self.saved

    def test_vdev_map_from_case(self):
        common.CASE = {'pool': {'devices': [{'path': '/dev/a', 'vdev': 0}, {'path': '/dev/b', 'vdev': 0},
                                            {'path': '/dev/c', 'vdev': 1}]}}
        self.assertIn("ZSREAD_VDEVS='0=/dev/a,/dev/b;1=/dev/c'", common.reader_cmd('batch'))

    def test_case_without_vdev_ids_keeps_the_device_list(self):
        common.CASE = {'pool': {'devices': [{'path': '/dev/a'}, {'path': '/dev/b'}]}}
        self.assertIn('ZSREAD_DEVS=/dev/a:/dev/b', common.reader_cmd('batch'))


class Parsing(unittest.TestCase):
    def test_parse_vdevs(self):
        self.assertEqual(zsread.parse_vdevs('0=/a,/b;1=/c'), {0: ['/a', '/b'], 1: ['/c']})
        self.assertEqual(zsread.parse_vdevs(''), {})

    def test_parse_offset(self):
        self.assertEqual(zsread.parse_offset('3496000'), (0, 0x3496000))
        self.assertEqual(zsread.parse_offset('1:840000'), (1, 0x840000))


@unittest.skipUnless(fx.have_images('modern-stripe') and fx.ensure_built(), 'needs gcc and the modern-stripe images')
class StripePool(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = fx.tmpdir()
        cls.d0, cls.d1 = fx.unpack('modern-stripe', cls.dir)
        s = fx.sidecars()['modern-stripe']
        tg = s['datasets'][s['target']]
        cls.dva = tg['dvas'][0]
        cls.psize = tg['psize']

    @classmethod
    def tearDownClass(cls):
        fx.rmtree(cls.dir)

    def open(self, vdevs=None, paths=None):
        dev = zsread.Dev(paths=paths, vdevs=vdevs)
        os_ = zsread.open_candidate(dev, 'OBJSET', LABEL_START + self.dva['offset'], 'off', self.psize, self.psize,
                                    self.dva['vdev'])
        z = zsread.ZPL(os_)
        self.os, self.zpl, self.dev = os_, z, dev
        return set(z.listdir(z.root))

    def big_file(self):
        """(sha256 of /video/big.mts, top-level vdevs its data blocks lie on)"""
        import hashlib
        z = self.zpl
        obj = z.listdir(z.listdir(z.root)['video'][0])['big.mts'][0]
        dn = self.os.dnode(obj)
        size = z.attrs(dn)['size']
        h, used = hashlib.sha256(), set()
        for blk in range((size + dn.blksz - 1) // dn.blksz):
            bp = dn.l0_bp(self.dev, blk)
            used |= {v for v, _, _ in bp.dvas}
            h.update(dn.read_block(self.dev, blk)[:size - blk * dn.blksz])
        return h.hexdigest(), used

    def test_file_data_needs_both_vdevs(self):
        """Metadata has two copies, one per vdev, so the tree is readable from one vdev; file data has one
        copy and is spread over both. The test is meaningful only if that is the case here."""
        want = next(f['sha256'] for f in fx.sidecars()['modern-stripe']['files'] if f['path'] == '/video/big.mts')
        self.open(vdevs={0: [self.d0], 1: [self.d1]})
        sha, used = self.big_file()
        self.assertEqual(sha, want)
        self.assertEqual(used, {0, 1})
        self.open(vdevs={self.dva['vdev']: [self.d0 if self.dva['vdev'] == 0 else self.d1]})
        with self.assertRaises(zsread.ReadError):
            self.big_file()

    def test_right_map_reads_the_tree(self):
        self.assertEqual(self.open(vdevs={0: [self.d0], 1: [self.d1]}), {'.cache', 'a', 'bin', 'docs', 'photos', 'video'})

    def test_swapped_map_is_refused_not_guessed(self):
        with self.assertRaises(Exception):      # the candidate itself is read from the wrong device: garbage
            self.open(vdevs={0: [self.d1], 1: [self.d0]})

    def test_plain_device_list_still_works(self):
        """Without a vdev map every device is tried (compatible with existing case files)."""
        self.assertEqual(self.open(paths=[self.d0, self.d1]), {'.cache', 'a', 'bin', 'docs', 'photos', 'video'})

    def test_zscan_max_vdev(self):
        def count(*opts):
            out = subprocess.run([fx.ZSCAN, *opts, self.d0], capture_output=True, text=True).stdout
            return sum(1 for l in out.splitlines() if l.split('\t')[1] in ('IND', 'OBJSET'))
        self.assertLess(count(), count('--max-vdev', '1'))      # pointers to vdev 1 are no longer rejected
        r = subprocess.run([fx.ZSCAN, '--max-vdev', '256', self.d0], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)


if __name__ == '__main__':
    unittest.main()
