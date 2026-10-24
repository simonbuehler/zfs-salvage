"""zvols as raw images: the sparse stream, its tar format, and the real fixture."""
import hashlib
import io
import os
import shutil
import subprocess
import tarfile
import unittest

import fx  # noqa: I001 - first: puts scripts/ on the path
import zsread

BS = 16384


class FakeDnode:
    """Enough of a dnode for ZvolStream: block size, the non-hole block ids, and block contents."""

    def __init__(self, blocks, bad=()):
        self.blksz, self.blocks, self.badset = BS, blocks, set(bad)

    def data_blocks(self, dev, limit, bad):
        return iter(sorted(b for b in self.blocks if b < limit))

    def read_block(self, dev, blk):
        if blk in self.badset:
            raise zsread.ReadError('no valid copy')
        return self.blocks.get(blk, bytes(BS))


class FakeOs:
    dev = None


def image(blocks, size):
    out = bytearray(size)
    for b, data in blocks.items():
        out[b * BS:b * BS + len(data)] = data[:max(0, size - b * BS)]
    return bytes(out)


def blk(fill):
    return bytes([fill]) * BS


def drain(stream, step):
    out = b''
    while True:
        chunk = stream.read(step)
        if not chunk:
            return out
        out += chunk


class ZvolStreamLogic(unittest.TestCase):
    def test_plain_volume_without_holes(self):
        blocks = {i: blk(i + 1) for i in range(4)}
        vs = zsread.ZvolStream(FakeOs(), FakeDnode(blocks), 4 * BS)
        self.assertFalse(vs.sparse)
        self.assertEqual((vs.header, vs.member_size), (b'', 4 * BS))
        self.assertEqual(drain(vs, 5000), image(blocks, 4 * BS))
        self.assertEqual(vs.sha.hexdigest(), hashlib.sha256(image(blocks, 4 * BS)).hexdigest())

    def test_sparse_map_and_data(self):
        blocks = {0: blk(1), 1: blk(2), 5: blk(3), 9: blk(4)}            # runs: [0,2) [5,6) [9,10)
        size = 11 * BS                                                  # one trailing block is a hole
        vs = zsread.ZvolStream(FakeOs(), FakeDnode(blocks), size)
        self.assertTrue(vs.sparse)
        self.assertEqual(vs.runs, [(0, 2), (5, 1), (9, 1)])
        text = vs.header.rstrip(b'\0').decode()
        self.assertEqual(text, f'4\n0\n{2 * BS}\n{5 * BS}\n{BS}\n{9 * BS}\n{BS}\n{11 * BS}\n0\n')   # closing entry at the real size
        self.assertEqual(len(vs.header) % 512, 0)
        self.assertEqual(vs.member_size, len(vs.header) + 4 * BS)
        stream = drain(vs, 7777)
        self.assertEqual(len(stream), vs.member_size)
        self.assertEqual(stream[len(vs.header):], blk(1) + blk(2) + blk(3) + blk(4))
        self.assertEqual(vs.sha.hexdigest(), hashlib.sha256(image(blocks, size)).hexdigest())   # logical image

    def test_last_block_is_clipped_to_the_volume_size(self):
        size = 2 * BS + 1000
        blocks = {0: blk(1), 2: blk(9)}
        vs = zsread.ZvolStream(FakeOs(), FakeDnode(blocks), size)
        self.assertEqual(vs.runs, [(0, 1), (2, 1)])
        self.assertEqual(drain(vs, 4096)[len(vs.header):], blk(1) + blk(9)[:1000])
        self.assertEqual(vs.sha.hexdigest(), hashlib.sha256(image(blocks, size)).hexdigest())

    def test_unreadable_block_is_zeroed_and_reported(self):
        blocks = {0: blk(1), 1: blk(2), 2: blk(3)}
        vs = zsread.ZvolStream(FakeOs(), FakeDnode(blocks, bad={1}), 3 * BS)
        data = drain(vs, 10000)
        self.assertEqual(data, blk(1) + bytes(BS) + blk(3))
        self.assertEqual(vs.bad, [1])

    def test_tar_stops_before_the_trailing_hole_is_hashed(self):
        """A reader that takes exactly member_size bytes (as tarfile does) leaves the hash incomplete until
        finish(): the regression behind the manifest hash of volumes whose end was never written."""
        blocks = {0: blk(1)}
        size = 6 * BS
        vs = zsread.ZvolStream(FakeOs(), FakeDnode(blocks), size)
        got = b''
        while len(got) < vs.member_size:
            got += vs.read(min(8192, vs.member_size - len(got)))
        self.assertNotEqual(vs.sha.hexdigest(), hashlib.sha256(image(blocks, size)).hexdigest())
        vs.finish()
        self.assertEqual(vs.sha.hexdigest(), hashlib.sha256(image(blocks, size)).hexdigest())

    def test_all_holes(self):
        vs = zsread.ZvolStream(FakeOs(), FakeDnode({}), 3 * BS)
        self.assertTrue(vs.sparse)
        self.assertEqual(drain(vs, 4096), vs.header)
        self.assertEqual(vs.header.rstrip(b'\0').decode(), f'1\n{3 * BS}\n0\n')
        self.assertEqual(vs.sha.hexdigest(), hashlib.sha256(bytes(3 * BS)).hexdigest())


def sparse_supported(directory):
    p = os.path.join(directory, 'probe')
    with open(p, 'wb') as f:
        f.truncate(8 << 20)
        f.write(b'x')
    return os.stat(p).st_blocks * 512 < (4 << 20)


class TarFormat(unittest.TestCase):
    def build(self, blocks, size, bad=()):
        vs = zsread.ZvolStream(FakeOs(), FakeDnode(blocks, bad), size)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w|', format=tarfile.PAX_FORMAT) as tar:
            tar.addfile(zsread.zvol_tarinfo('/volume.raw', vs), vs)
        vs.finish()
        return buf.getvalue(), vs

    def test_python_reads_it_back(self):
        blocks = {0: blk(1), 5: blk(3), 9: blk(4)}
        data, vs = self.build(blocks, 10 * BS)
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            (m,) = tf.getmembers()
            self.assertEqual((m.name, m.size), ('volume.raw', 10 * BS))
            self.assertEqual(tf.extractfile(m).read(), image(blocks, 10 * BS))

    @unittest.skipUnless(shutil.which('tar'), 'no tar')
    def test_gnu_tar_extracts_it_and_keeps_the_holes(self):
        blocks = {0: blk(1), 2000: blk(3)}                          # 32 MiB volume, two data blocks
        size = 2048 * BS
        data, vs = self.build(blocks, size)
        d = fx.tmpdir()
        try:
            r = subprocess.run(['tar', '-x', '-f', '-', '-C', d], input=data, capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            path = os.path.join(d, 'volume.raw')
            self.assertEqual(os.path.getsize(path), size)
            with open(path, 'rb') as f:
                self.assertEqual(hashlib.sha256(f.read()).hexdigest(), vs.sha.hexdigest())
            if sparse_supported(d):
                self.assertLess(os.stat(path).st_blocks * 512, 2 << 20, 'holes were written out as zeros')
        finally:
            fx.rmtree(d)

    def test_plain_volume_is_a_plain_tar_member(self):
        blocks = {i: blk(i + 1) for i in range(3)}
        data, vs = self.build(blocks, 3 * BS)
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            (m,) = tf.getmembers()
            self.assertEqual((m.name, m.size, m.issparse()), ('volume.raw', 3 * BS, False))


@unittest.skipUnless(fx.have_images('modern-zvol'), 'needs the modern-zvol images')
class RealFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = fx.tmpdir()
        cls.dev = zsread.Dev(fx.unpack('modern-zvol', cls.dir))
        s = fx.sidecars()['modern-zvol']
        tg = s['datasets'][s['target']]
        cls.want = s['zvol']
        cls.os = zsread.open_candidate(cls.dev, 'OBJSET', s['label_start'] + tg['dvas'][0]['offset'], 'off',
                                       tg['psize'], tg['lsize'])

    @classmethod
    def tearDownClass(cls):
        fx.rmtree(cls.dir)

    def test_objset_is_a_zvol_of_the_right_size(self):
        self.assertEqual(self.os.os_type, 3)
        self.assertEqual(zsread.zvol_size(self.os), self.want['size'])

    def test_the_hole_is_found_in_the_block_tree(self):
        dn = self.os.dnode(zsread.ZVOL_DATA_OBJ)
        ids = list(dn.data_blocks(self.dev, self.want['size'] // dn.blksz, []))
        (hs, hl), = self.want['holes']
        self.assertTrue(ids)
        self.assertFalse([b for b in ids if hs <= b * dn.blksz < hs + hl], 'blocks inside the hole')
        self.assertLess(len(ids) * dn.blksz, self.want['size'] - hl + dn.blksz)

    def test_extraction_with_gnu_tar_is_sparse_and_identical(self):
        dn = self.os.dnode(zsread.ZVOL_DATA_OBJ)
        vs = zsread.ZvolStream(self.os, dn, self.want['size'])
        self.assertTrue(vs.sparse)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w|', format=tarfile.PAX_FORMAT) as tar:
            tar.addfile(zsread.zvol_tarinfo('/volume.raw', vs), vs)
        vs.finish()
        self.assertEqual(vs.sha.hexdigest(), self.want['sha256'])
        self.assertEqual(vs.bad, [])
        d = fx.tmpdir()
        try:
            r = subprocess.run(['tar', '-x', '-f', '-', '-C', d], input=buf.getvalue(), capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            path = os.path.join(d, 'volume.raw')
            with open(path, 'rb') as f:
                self.assertEqual(hashlib.sha256(f.read()).hexdigest(), self.want['sha256'])
            if sparse_supported(d):
                self.assertLess(os.stat(path).st_blocks * 512, self.want['size'] - self.want['holes'][0][1] + (1 << 20))
        finally:
            fx.rmtree(d)


@unittest.skipUnless(fx.have_images('modern-zvol') and fx.ensure_built(), 'needs gcc and the modern-zvol images')
class RankTreesWithAZvolTarget(unittest.TestCase):
    """rank-trees recognises a zvol target by its type (and size), not by directory names."""

    @classmethod
    def setUpClass(cls):
        import gzip
        cls.dir = fx.tmpdir()
        cls.dev = fx.unpack('modern-zvol', os.path.join(cls.dir, 'img'))[0]
        cls.side = fx.sidecars()['modern-zvol']
        cls.raw = os.path.join(cls.dir, 'raw')
        os.makedirs(cls.raw)
        scan = subprocess.run([fx.ZSCAN, cls.dev], capture_output=True, text=True).stdout
        with gzip.open(os.path.join(cls.raw, 'chunk_0000.tsv.gz'), 'wt') as f:
            f.write(scan)
        open(os.path.join(cls.raw, 'chunk_0000.tsv.gz.done'), 'w').close()
        zdir = os.path.join(cls.dir, 'zs')
        os.makedirs(zdir)
        shutil.copy(os.path.join(fx.SCRIPTS, 'zsread.py'), zdir)
        shutil.copy(fx.LIB, zdir)
        cls.reader = os.path.join(zdir, 'zsread.py')

    @classmethod
    def tearDownClass(cls):
        fx.rmtree(cls.dir)

    def rank(self, target):
        import json
        import sys
        from test_visible_errors import env_for
        state = os.path.join(self.dir, 'state-' + str(len(os.listdir(self.dir))))
        os.makedirs(os.path.join(state, 'status'))
        case = {'name': 't', 'pool': {'name': 'p', 'devices': [{'path': self.dev, 'vdev': 0}]},
                'profile': {'label_start': self.side['label_start'], 'compression': [], 'checksum': []},
                'timeline': [{'txg': 100, 'time': 1}], 'target': target,
                'scan': {'device': 'd0', 'chunk_dir': os.path.relpath(self.raw, fx.REPO), 'log': 'x.log'}}
        cpath = os.path.join(state, 'case.json')
        with open(cpath, 'w') as f:
            json.dump(case, f)
        e = env_for(state, 'local', ZFS_SALVAGE_CASE=cpath, ZFS_SALVAGE_READER=self.reader)
        r = subprocess.run([sys.executable, '-I', os.path.join(fx.SCRIPTS, 'rank-trees.py'), '40'], env=e,
                           capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        with open(os.path.join(state, 'status', 'target_candidates.json')) as f:
            return json.load(f)

    def test_zvol_target_gets_zvol_states(self):
        got = self.rank({'dataset': 'p/vol', 'root_names': [], 'type': 'zvol'})
        self.assertTrue(got)
        self.assertTrue(all(g['root'].startswith('zvol, ') for g in got))
        self.assertTrue(got[0]['candidate'])

    def test_volume_size_selects_among_zvols(self):
        self.assertTrue(self.rank({'dataset': 'p/vol', 'root_names': [], 'type': 'zvol',
                                   'volsize': self.side['zvol']['size']}))
        self.assertEqual(self.rank({'dataset': 'p/vol', 'root_names': [], 'type': 'zvol', 'volsize': 12345}), [])

    def test_a_file_system_target_does_not_pick_the_zvol(self):
        self.assertEqual(self.rank({'dataset': 'p/home', 'root_names': ['docs'], 'type': 'zpl'}), [])


if __name__ == '__main__':
    unittest.main()
