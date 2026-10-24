"""zscan with indirect blocks of different sizes and 2/4 KiB objset blocks.

Pools created by current ZFS use 128 KiB indirect blocks for the meta-dnode tree; pools from ZFS 0.6
(like the case this project started with) use 16 KiB. No fixture can be made with 16 KiB blocks today,
so that path is covered by hand-made blocks compressed with the independent lzjb encoder of
test_codecs.
"""
import os
import re
import struct
import subprocess
import unittest

import fx
from test_codecs import lzjb_encode

BUILT = fx.ensure_built()
MIB = 1 << 20


def indirect_block(lsize, level, n, birth=50):
    """An indirect block of the meta-dnode tree: n block pointers of type DNODE on `level`, rest zero."""
    b = bytearray(lsize)
    prop = (31) | (7 << 16) | (3 << 32) | (7 << 40) | (10 << 48) | (level << 56)   # 16K L / 4K P, lzjb, fletcher4
    for k in range(n):
        w = [0] * 16
        w[0], w[1] = 8, 0x100 + 8 * k                  # DVA: vdev 0, 4 KiB at sector 0x100 + 8k
        w[6], w[10], w[11] = prop, birth + k, 32
        struct.pack_into('<16Q', b, 128 * k, *w)
    return bytes(b)


def zscan(path, *opts):
    r = subprocess.run([fx.ZSCAN, *opts, path], capture_output=True, text=True)
    return {int(l.split('\t')[0], 16): l.split('\t') for l in r.stdout.splitlines()}


@unittest.skipUnless(BUILT, 'no C compiler: zscan could not be built')
class IndirectSizes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = fx.tmpdir()
        cls.img = os.path.join(cls.dir, 'sizes.img')
        cls.blocks = {
            0x100000: indirect_block(16384, 4, 20),            # 16 KiB, zeros behind it
            0x200000: indirect_block(16384, 3, 40),            # 16 KiB, directly followed by ...
            None: indirect_block(16384, 2, 10, birth=90),      # ... another 16 KiB block (placed below)
            0x300000: indirect_block(131072, 4, 700),          # 128 KiB with 700 of 1024 entries in use
        }
        cls.packed = {}
        with open(cls.img, 'wb') as f:
            f.truncate(4 * MIB)
            for off, data in cls.blocks.items():
                if off is None:
                    continue
                z = lzjb_encode(data)
                cls.packed[off] = z
                f.seek(off)
                f.write(z)
            follow = 0x200000 + (len(cls.packed[0x200000]) + 511) // 512 * 512
            z = lzjb_encode(cls.blocks[None])
            cls.packed[follow] = z
            cls.follow = follow
            f.seek(follow)
            f.write(z)

    @classmethod
    def tearDownClass(cls):
        fx.rmtree(cls.dir)

    def ind(self, rows, off):
        r = rows.get(off)
        self.assertIsNotNone(r, f'nothing found at {off:#x}')
        self.assertEqual(r[1], 'IND')
        return int(r[3]), int(r[4]), int(r[6].split('=')[1])          # psize, lsize, child level

    def psize(self, off):
        return (len(self.packed[off]) + 511) // 512 * 512

    def test_default_reports_each_block_with_its_real_size(self):
        rows = zscan(self.img)
        self.assertEqual(self.ind(rows, 0x100000), (self.psize(0x100000), 16384, 4))
        self.assertEqual(self.ind(rows, 0x200000), (self.psize(0x200000), 16384, 3))
        self.assertEqual(self.ind(rows, self.follow), (self.psize(self.follow), 16384, 2))
        self.assertEqual(self.ind(rows, 0x300000), (self.psize(0x300000), 131072, 4))

    def test_only_16k(self):
        """With --ind-size 16384 (a 0.6-era pool) a 128 KiB block shows up as its first 16 KiB, as in older versions."""
        rows = zscan(self.img, '--ind-size', '16384')
        self.assertEqual(self.ind(rows, 0x100000)[1], 16384)
        self.assertEqual(self.ind(rows, 0x300000)[1], 16384)

    def test_only_128k(self):
        rows = zscan(self.img, '--ind-size', '131072')
        self.assertNotIn(0x100000, rows)
        self.assertNotIn(0x200000, rows)
        self.assertEqual(self.ind(rows, 0x300000)[1], 131072)

    def test_entries_counted_over_the_whole_block(self):
        rows = zscan(self.img)
        self.assertEqual(rows[0x300000][7], 'entries=700')
        self.assertEqual(rows[0x100000][7], 'entries=20')

    def test_bad_ind_size(self):
        for v in ('1000', '262144', '0', 'x'):
            r = subprocess.run([fx.ZSCAN, '--ind-size', v, self.img], capture_output=True, text=True)
            self.assertEqual(r.returncode, 2, v)


@unittest.skipUnless(BUILT and fx.have_images('legacy-single'), 'needs gcc and the legacy-single images')
class FixtureSizes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = fx.tmpdir()
        cls.dev = fx.unpack('legacy-single', cls.dir)[0]
        cls.rows = list(zscan(cls.dev).values())

    @classmethod
    def tearDownClass(cls):
        fx.rmtree(cls.dir)

    def test_indirect_blocks_are_128k(self):
        sizes = {r[4] for r in self.rows if r[1] == 'IND'}
        self.assertEqual(sizes, {'131072'})

    def test_objset_block_matches_the_checksum_zfs_stored(self):
        """Right psize and lsize: zscan's fletcher4 of the objset block equals the rootbp checksum."""
        s = fx.sidecars()['legacy-single']
        tg = s['datasets'][s['target']]
        want = re.search(r'cksum=(\S+)', tg['rootbp']).group(1)
        by_off = {int(r[0], 16): r for r in self.rows}
        for dva in tg['dvas']:
            r = by_off.get(s['label_start'] + dva['offset'])
            self.assertIsNotNone(r)
            self.assertEqual((r[1], r[3], r[4], r[5]), ('OBJSET', str(tg['psize']), str(tg['lsize']), want))


if __name__ == '__main__':
    unittest.main()
