"""zscan's TXG plausibility limit (--max-txg) instead of a constant compiled for one pool."""
import os
import re
import struct
import subprocess
import unittest

import fx

BUILT = fx.ensure_built()


def zscan(path, *opts):
    r = subprocess.run([fx.ZSCAN, *opts, path], capture_output=True, text=True)
    return r.returncode, [l.split('\t') for l in r.stdout.splitlines()], r.stderr


def birth_of(row):
    """The newest TXG a find refers to: OBJSET bp_birth, IND/DNODES max_birth."""
    kv = dict(x.split('=', 1) for x in row[6:] if '=' in x)
    return int(kv.get('bp_birth') or kv.get('max_birth') or 0)


def objset_block(birth, devoff_sectors=16):
    """A hand-made, uncompressed 16 KiB objset block (what zscan accepts as OBJSET): dnode 0 of type
    DMU_OT_DNODE with a plausible block pointer to a 6-level tree, os_type = ZFS."""
    b = bytearray(16384)
    b[0], b[1], b[2], b[3] = 10, 17, 6, 3                 # type dnode, 128 KiB indirect blocks, 6 levels, 3 bps
    b[5], b[6] = 7, 2                                      # fletcher4, uncompressed
    struct.pack_into('<H', b, 8, 32)                       # 16 KiB data blocks
    prop = (31) | (31 << 16) | (2 << 32) | (7 << 40) | (10 << 48) | (5 << 56)
    words = [0] * 16
    words[0], words[1] = 8, devoff_sectors                 # DVA 0: vdev 0, 4 KiB at sector devoff
    words[6], words[10], words[11] = prop, birth, 1
    struct.pack_into('<16Q', b, 64, *words)
    struct.pack_into('<Q', b, 704, 2)                      # os_type = ZFS
    return bytes(b)


@unittest.skipUnless(BUILT, 'no C compiler: zscan could not be built')
class SyntheticBlocks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = fx.tmpdir()
        cls.img = os.path.join(cls.dir, 'synthetic.img')
        with open(cls.img, 'wb') as f:
            f.truncate(1 << 20)
            f.seek(0x20000)
            f.write(objset_block(5_000_000))               # far above the old fixed limit of 800,000
            f.seek(0x40000)
            f.write(objset_block(100))

    @classmethod
    def tearDownClass(cls):
        fx.rmtree(cls.dir)

    def births(self, *opts):
        rc, rows, _ = zscan(self.img, *opts)
        self.assertEqual(rc, 0)
        return sorted(birth_of(r) for r in rows if r[1] == 'OBJSET')

    def test_default_has_no_practical_limit(self):
        self.assertEqual(self.births(), [100, 5_000_000])

    def test_limit_is_inclusive(self):
        self.assertEqual(self.births('--max-txg', '5000000'), [100, 5_000_000])
        self.assertEqual(self.births('--max-txg', '4999999'), [100])
        self.assertEqual(self.births('--max-txg', '100'), [100])
        self.assertEqual(self.births('--max-txg', '99'), [])

    def test_hex_and_position_of_option(self):
        self.assertEqual(self.births('--max-txg', '0x7d'), [100])           # 125

    def test_bad_arguments(self):
        for args in (['--max-txg'], ['--max-txg', 'abc', self.img], ['--bogus', self.img], []):
            r = subprocess.run([fx.ZSCAN, *args], capture_output=True, text=True)
            self.assertEqual(r.returncode, 2, args)
            self.assertEqual(r.stdout, '')

    def test_positional_range_still_works(self):
        """`zscan <device> start end` is what scan-device.sh uses for each chunk."""
        rc, rows, _ = zscan(self.img, '--max-txg', '1000000')
        self.assertEqual(rc, 0)
        r = subprocess.run([fx.ZSCAN, '--max-txg', '1000000', self.img, '0x30000', '0x80000'],
                           capture_output=True, text=True)
        self.assertEqual([birth_of(l.split('\t')) for l in r.stdout.splitlines() if '\tOBJSET\t' in l], [100])


@unittest.skipUnless(BUILT and fx.have_images('legacy-single'), 'needs gcc and the legacy-single images')
class FixtureLimits(unittest.TestCase):
    """The limit on a real pool: only finds born at or before it are reported."""

    @classmethod
    def setUpClass(cls):
        cls.dir = fx.tmpdir()
        cls.dev = fx.unpack('legacy-single', cls.dir)[0]
        cls.newest = fx.sidecars()['legacy-single']['datasets']['fx-legacy-single/home']['birth']     # 41

    @classmethod
    def tearDownClass(cls):
        fx.rmtree(cls.dir)

    def test_births_never_exceed_the_limit(self):
        counts = {}
        for limit in (3, 20, self.newest - 1, None):
            opts = ['--max-txg', str(limit)] if limit else []
            rc, rows, _ = zscan(self.dev, *opts)
            self.assertEqual(rc, 0)
            counts[limit] = len([r for r in rows if r[1] in ('OBJSET', 'IND')])
            if limit:
                self.assertTrue(all(birth_of(r) <= limit for r in rows if r[1] in ('OBJSET', 'IND')), limit)
        self.assertEqual(counts[3], 0)                                  # the oldest bp of the pool was born in TXG 4
        self.assertLess(counts[3], counts[20])
        self.assertLess(counts[20], counts[self.newest - 1])
        self.assertLess(counts[self.newest - 1], counts[None])
        print(f'\n    legacy-single OBJSET+IND finds by limit: {counts}')

    def test_newest_state_needs_the_limit_to_reach_it(self):
        _, rows, _ = zscan(self.dev, '--max-txg', str(self.newest - 1))
        self.assertFalse([r for r in rows if r[1] == 'OBJSET' and birth_of(r) == self.newest])
        _, rows, _ = zscan(self.dev, '--max-txg', str(self.newest))
        self.assertTrue([r for r in rows if r[1] == 'OBJSET' and birth_of(r) == self.newest])


@unittest.skipUnless(BUILT and fx.have_images('modern-single'), 'needs gcc and the modern-single images')
class ModernPoolRoots(unittest.TestCase):
    """lz4 pool (ZFS 2.2): the tree roots are found, and the limit hides the younger ones."""

    @classmethod
    def setUpClass(cls):
        cls.dir = fx.tmpdir()
        cls.dev = fx.unpack('modern-single', cls.dir)[0]
        s = fx.sidecars()['modern-single']
        cls.levels = s['meta_dnode']['nlevels']
        cls.birth = s['datasets'][s['target']]['birth']           # objset block birth = newest state

    @classmethod
    def tearDownClass(cls):
        fx.rmtree(cls.dir)

    def roots(self, *opts):
        _, rows, _ = zscan(self.dev, *opts)
        return [r for r in rows if r[1] == 'IND' and r[6] == f'child_level={self.levels - 2}']

    def test_newest_root_is_found_and_limited(self):
        default = self.roots()
        self.assertTrue(default)
        self.assertEqual({(r[2], r[4]) for r in default}, {('lz4', '131072')})
        self.assertEqual(max(birth_of(r) for r in default), self.birth)
        below = self.roots('--max-txg', str(self.birth - 1))
        self.assertLess(len(below), len(default))
        self.assertTrue(all(birth_of(r) < self.birth for r in below))
        self.assertEqual(self.roots('--max-txg', '5'), [])


class NoCaseConstants(unittest.TestCase):
    def test_no_pool_specific_txg_constants_in_code(self):
        bad = []
        for sub in ('tools', 'scripts'):
            for root, _, files in os.walk(os.path.join(fx.REPO, sub)):
                for name in files:
                    if name.endswith(('.c', '.py', '.sh', '.html', '.js')):
                        with open(os.path.join(root, name), encoding='utf-8', errors='replace') as f:
                            text = f.read()
                        bad += [f'{name}: {m}' for m in re.findall(r'\b(?:800000|700000)\b', text)]
        self.assertEqual(bad, [])


if __name__ == '__main__':
    unittest.main()
