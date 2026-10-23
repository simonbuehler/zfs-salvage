"""Codec tests: the Python implementations in zsread.py and the C ones in libzsalvage.so (and the
checksum inside zscan) must agree with hand-computed vectors, with each other, and with the checksums
ZFS itself stored in the fixture pools.

Tests that need the fixture images or a C compiler skip themselves when those are missing.
"""
import ctypes
import os
import random
import re
import subprocess
import unittest

import fx
import zsread

LABEL_START = 4 << 20
M64 = (1 << 64) - 1


def load_lib():
    if not fx.ensure_built():
        return None
    lib = ctypes.CDLL(fx.LIB)
    lib.zs_fletcher4.argtypes = [ctypes.c_char_p, ctypes.c_uint64, ctypes.POINTER(ctypes.c_uint64)]
    lib.zs_fletcher4.restype = None
    lib.zs_lzjb.argtypes = [ctypes.c_char_p, ctypes.c_uint64, ctypes.c_char_p, ctypes.c_uint64]
    lib.zs_lzjb.restype = ctypes.c_int64
    lib.zs_read_verify.argtypes = [ctypes.c_int, ctypes.c_uint64, ctypes.c_uint64,
                                   ctypes.POINTER(ctypes.c_uint64), ctypes.c_int, ctypes.c_char_p, ctypes.c_uint64]
    lib.zs_read_verify.restype = ctypes.c_int
    return lib


LIB = load_lib()


def c_fletcher4(data):
    out = (ctypes.c_uint64 * 4)()
    LIB.zs_fletcher4(data, len(data), out)
    return tuple(out)


def c_lzjb(src, dlen):
    """Decompressed bytes, or None if the C decoder rejects the stream."""
    dst = ctypes.create_string_buffer(max(dlen, 1))
    n = LIB.zs_lzjb(src, len(src), dst, dlen)
    return None if n < 0 else dst.raw[:n]


def py_lzjb(src, dlen):
    try:
        return zsread.lzjb(src, dlen)
    except zsread.ReadError:
        return None


# A hand-made lzjb stream: copymap 0b00000100 -> items 0 and 1 are literals 'A' 'B', item 2 is a copy
# of length 6 (byte0 = (6-3) << 2 | offset >> 8) from distance 2, so the output is 'ABABABAB'.
LZJB_VECTOR = (bytes([0x04, ord('A'), ord('B'), (6 - 3) << 2, 2]), 8, b'ABABABAB')


def lzjb_encode(data):
    """A small independent lzjb encoder (greedy, trigram index) used to make valid test streams."""
    out, i, n = bytearray(), 0, len(data)
    copymask, map_at = 1 << 7, 0
    seen = {}
    while i < n:
        copymask <<= 1
        if copymask == 1 << 8:
            copymask, map_at = 1, len(out)
            out.append(0)
        best_len = best_off = 0
        if i + 3 <= n:
            for j in reversed(seen.get(data[i:i + 3], [])[-16:]):
                off = i - j
                if off > 1023:
                    break
                k = 0
                while k < 66 and i + k < n and data[i + k - off] == data[i + k]:
                    k += 1
                if k > best_len:
                    best_len, best_off = k, off
        step = best_len if best_len >= 3 else 1
        if best_len >= 3:
            out[map_at] |= copymask
            out += bytes([((best_len - 3) << 2) | (best_off >> 8), best_off & 0xff])
        else:
            out.append(data[i])
        for j in range(i, min(i + step, n - 2)):
            seen.setdefault(data[j:j + 3], []).append(j)
        i += step
    return bytes(out)


def sample_text(seed, size):
    rnd = random.Random(seed)
    words = [b'alpha', b'beta', b'gamma', b'delta', b'photo', b'zfs ', b'disk\n', b'0123456789']
    out = bytearray()
    while len(out) < size:
        out += rnd.choice(words) if rnd.random() < 0.9 else rnd.randbytes(rnd.randint(1, 6))
    return bytes(out[:size])


class KnownAnswers(unittest.TestCase):
    """Values worked out by hand; no fixtures, no C needed."""

    def test_fletcher4_python(self):
        self.assertEqual(zsread.fletcher4(b''), (0, 0, 0, 0))
        self.assertEqual(zsread.fletcher4((1).to_bytes(4, 'little')), (1, 1, 1, 1))
        # words 1, 2: a=3, b=1+3=4, c=1+4=5, d=1+5... computed step by step below
        a = b = c = d = 0
        for w in (1, 2):
            a += w
            b += a
            c += b
            d += c
        self.assertEqual((a, b, c, d), (3, 4, 5, 6))
        self.assertEqual(zsread.fletcher4((1).to_bytes(4, 'little') + (2).to_bytes(4, 'little')), (3, 4, 5, 6))

    def test_fletcher4_wraps_at_64_bit(self):
        words = [0xffffffff] * 300000
        a = b = c = d = 0
        for w in words:                              # exact integers, reduced afterwards
            a += w
            b += a
            c += b
            d += c
        data = b''.join(w.to_bytes(4, 'little') for w in words)
        self.assertGreater(d, M64)                    # the test only means something if it overflows
        self.assertEqual(zsread.fletcher4(data), (a & M64, b & M64, c & M64, d & M64))

    def test_lzjb_python_vector(self):
        src, dlen, want = LZJB_VECTOR
        self.assertEqual(zsread.lzjb(src, dlen), want)
        self.assertEqual(zsread.lzjb(src, 4), want[:4])          # stops at the requested length
        self.assertEqual(zsread.lzjb(src, 0), b'')

    def test_lzjb_python_roundtrip(self):
        for seed, size in ((1, 100), (2, 3000), (3, 16384)):
            data = sample_text(seed, size)
            packed = lzjb_encode(data)
            self.assertLess(len(packed), len(data))               # the sample does compress
            self.assertEqual(zsread.lzjb(packed, size), data)

    def test_lzjb_python_rejects_garbage(self):
        for bad in (b'', bytes([0x01, 0x00, 0x00]),             # copy item with offset 0
                    bytes([0x01, 0x00, 0x05]),                   # offset 5 > output so far
                    bytes([0x00, 0x41]),                         # 7 literals missing
                    bytes([0x04, 0x41, 0x42, 0x0c])):           # copy item cut off
            with self.assertRaises(zsread.ReadError, msg=bad.hex()):
                zsread.lzjb(bad, 8)


@unittest.skipIf(LIB is None, 'no C compiler: libzsalvage.so could not be built')
class PythonVersusC(unittest.TestCase):
    def test_fletcher4_random_buffers(self):
        rnd = random.Random(4)
        for size in (0, 4, 512, 4096, 16384, 131072, 1 << 20):
            data = rnd.randbytes(size)
            self.assertEqual(c_fletcher4(data), zsread.fletcher4(data), f'size {size}')

    def test_fletcher4_overflow_buffer(self):
        data = b'\xff' * (1 << 20)                              # drives every accumulator past 2^64
        self.assertEqual(c_fletcher4(data), zsread.fletcher4(data))

    def test_lzjb_vector(self):
        src, dlen, want = LZJB_VECTOR
        self.assertEqual(c_lzjb(src, dlen), want)
        self.assertEqual(c_lzjb(src, 4), want[:4])

    def test_lzjb_roundtrip(self):
        for seed, size in ((1, 100), (2, 3000), (3, 16384)):
            data = sample_text(seed, size)
            self.assertEqual(c_lzjb(lzjb_encode(data), size), data)

    def test_lzjb_mutated_streams_agree(self):
        """Valid streams, truncated or corrupted: both decoders accept the same inputs with the same output."""
        rnd = random.Random(11)
        base = lzjb_encode(sample_text(7, 4096))                 # a valid stream with literals and copies
        checked = accepted = 0
        for _ in range(3000):
            s = bytearray(base)
            kind = rnd.randrange(3)
            if kind == 0:
                del s[rnd.randrange(len(s)):]
            elif kind == 1:
                for _ in range(rnd.randint(1, 4)):
                    s[rnd.randrange(len(s))] = rnd.randrange(256)
            else:
                s = s[rnd.randrange(len(s)):]
            dlen = rnd.choice((16, 512, 2048, 4096))
            c, p = c_lzjb(bytes(s), dlen), py_lzjb(bytes(s), dlen)
            self.assertEqual(c, p, f'stream {bytes(s[:24]).hex()}... dlen {dlen}')
            checked += 1
            accepted += c is not None
        self.assertGreater(accepted, 100)                        # not all of them were rejected
        self.assertLess(accepted, checked)                       # and not all of them were accepted


@unittest.skipIf(LIB is None, 'no C compiler: libzsalvage.so could not be built')
class FixtureBlocks(unittest.TestCase):
    """Real blocks written by ZFS 2.2.2 into the fixture pools."""

    @classmethod
    def setUpClass(cls):
        cls.dir = fx.tmpdir()
        cls.dev = {}
        for v in ('legacy-single', 'modern-single', 'modern-mirror', 'modern-twodatasets'):
            if fx.have_images(v):
                cls.dev[v] = fx.unpack(v, os.path.join(cls.dir, v))[0]

    @classmethod
    def tearDownClass(cls):
        fx.rmtree(cls.dir)

    def objset_block(self, variant):
        s = fx.sidecars()[variant]
        d = s['datasets'][s['target']]
        m = re.search(r'cksum=(\w{16}):(\w{16}):(\w{16}):(\w{16})', d['rootbp'])
        words = tuple(int(x, 16) for x in m.groups())
        comp = d['objset_block'].split()[1]                     # 'lzjb' | 'uncompressed'
        off = LABEL_START + d['dvas'][0]['offset']
        with open(self.dev[variant], 'rb') as f:
            f.seek(off)
            raw = f.read(d['psize'])
        return d, words, comp, off, raw

    def test_stored_fletcher4_of_the_objset_block(self):
        """ZFS stored a checksum in the root block pointer; both implementations reproduce it."""
        for v in self.dev:
            with self.subTest(v):
                d, words, comp, off, raw = self.objset_block(v)
                self.assertEqual(zsread.fletcher4(raw), words)
                self.assertEqual(c_fletcher4(raw), words)

    def test_objset_block_decompression(self):
        d, words, comp, off, raw = self.objset_block('legacy-single') if 'legacy-single' in self.dev else (None,) * 5
        if d is None:
            self.skipTest('no legacy-single images')
        self.assertEqual(comp, 'lzjb')
        self.assertLess(d['psize'], d['lsize'])
        py, c = zsread.lzjb(raw, d['lsize']), c_lzjb(raw, d['lsize'])
        self.assertEqual(py, c)
        self.assertEqual(len(py), d['lsize'])
        self.assertEqual(py[0], 10)                              # dnode 0 of an objset: type DMU_OT_DNODE
        self.assertEqual(int.from_bytes(py[704:712], 'little'), 2)   # os_type ZFS

    def test_read_verify(self):
        """zs_read_verify: checksum, decompression, and the error codes, against a real block."""
        for v in self.dev:
            with self.subTest(v):
                d, words, comp, off, raw = self.objset_block(v)
                ck = (ctypes.c_uint64 * 4)(*words)
                code = 3 if comp == 'lzjb' else 2
                with open(self.dev[v], 'rb') as f:
                    dst = ctypes.create_string_buffer(d['lsize'])
                    self.assertEqual(LIB.zs_read_verify(f.fileno(), off, d['psize'], ck, code, dst, d['lsize']), 0)
                    want = zsread.lzjb(raw, d['lsize']) if comp == 'lzjb' else raw[:d['lsize']]
                    self.assertEqual(dst.raw, want)
                    bad = (ctypes.c_uint64 * 4)(words[0] ^ 1, *words[1:])
                    self.assertEqual(LIB.zs_read_verify(f.fileno(), off, d['psize'], bad, code, dst, d['lsize']), 1)
                    self.assertEqual(LIB.zs_read_verify(f.fileno(), off, d['psize'], ck, 99, dst, d['lsize']), 4)
                    far = os.path.getsize(self.dev[v]) + 4096          # beyond the end of the image
                    self.assertEqual(LIB.zs_read_verify(f.fileno(), far, d['psize'], ck, code, dst, d['lsize']), 2)

    def test_scanner_finds_agree_with_python(self):
        """zscan's own fletcher4 and lzjb against the Python ones, on every kind of find it reports."""
        if 'legacy-single' not in self.dev:
            self.skipTest('no legacy-single images')
        out = subprocess.run([fx.ZSCAN, self.dev['legacy-single']], capture_output=True, text=True).stdout
        rows = [l.split('\t') for l in out.splitlines()]
        self.assertGreater(len(rows), 100)
        n = {'lzjb': 0, 'off': 0}
        with open(self.dev['legacy-single'], 'rb') as f:
            for r in rows[:250]:
                off, comp, psize, lsize = int(r[0], 16), r[2], int(r[3]), int(r[4])
                f.seek(off)
                raw = f.read(psize)
                self.assertEqual(c_fletcher4(raw), tuple(int(x, 16) for x in r[5].split(':')), f'zscan @{r[0]}')
                self.assertEqual(zsread.fletcher4(raw), c_fletcher4(raw))
                if comp == 'lzjb':
                    self.assertEqual(py_lzjb(raw, lsize), c_lzjb(raw, lsize), f'lzjb @{r[0]}')
                    self.assertIsNotNone(c_lzjb(raw, lsize))
                n[comp] += 1
        self.assertGreater(n['lzjb'], 50)

    def test_real_blocks_mutated(self):
        """Corrupt real lzjb blocks: the decoders still agree."""
        if 'legacy-single' not in self.dev:
            self.skipTest('no legacy-single images')
        out = subprocess.run([fx.ZSCAN, self.dev['legacy-single']], capture_output=True, text=True).stdout
        rnd = random.Random(5)
        rows = [l.split('\t') for l in out.splitlines() if l.split('\t')[2] == 'lzjb'][:20]
        with open(self.dev['legacy-single'], 'rb') as f:
            for r in rows:
                f.seek(int(r[0], 16))
                raw = bytearray(f.read(int(r[3])))
                for _ in range(40):
                    s = bytearray(raw)
                    for _ in range(rnd.randint(1, 3)):
                        s[rnd.randrange(len(s))] = rnd.randrange(256)
                    self.assertEqual(py_lzjb(bytes(s), 16384), c_lzjb(bytes(s), 16384))


if __name__ == '__main__':
    unittest.main()
