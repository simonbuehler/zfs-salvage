"""Codec tests (fletcher4, sha256, lzjb, lz4): the Python implementations in zsread.py and the C ones in
libzsalvage.so (and those inside zscan) must agree with hand-computed vectors, with each other, and with
the checksums ZFS itself stored in the fixture pools.

Tests that need the fixture images or a C compiler skip themselves when those are missing.
"""
import ctypes
import os
import random
import re
import struct
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
    lib.zs_lz4.argtypes = [ctypes.c_char_p, ctypes.c_uint64, ctypes.c_char_p, ctypes.c_uint64]
    lib.zs_lz4.restype = ctypes.c_int64
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


def c_lz4(src, dlen):
    """Decoded bytes if the C decoder produces exactly dlen bytes, else None."""
    dst = ctypes.create_string_buffer(max(dlen, 1))
    n = LIB.zs_lz4(src, len(src), dst, dlen)
    return dst.raw[:n] if n == dlen else None


def py_lz4(src, dlen):
    try:
        return zsread.lz4(src, dlen)
    except zsread.ReadError:
        return None


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


def lz4_encode(data):
    """A small independent LZ4 block encoder (greedy, 4-byte hash), framed like ZFS: 4-byte big-endian
    length of the block, then the block. Last 12 bytes stay literals, as the format requires."""
    out, n, i, anchor, table = bytearray(), len(data), 0, 0, {}

    def length_bytes(v):
        while v >= 255:
            out.append(255)
            v -= 255
        out.append(v)

    def sequence(lit, match=None):
        llen = len(lit)
        mcode = 0 if match is None else match[1] - 4
        out.append((min(llen, 15) << 4) | min(mcode, 15))
        if llen >= 15:
            length_bytes(llen - 15)
        out.extend(lit)
        if match is not None:
            out.extend(match[0].to_bytes(2, 'little'))
            if mcode >= 15:
                length_bytes(mcode - 15)

    while i < n - 12:
        key = data[i:i + 4]
        j = table.get(key)
        table[key] = i
        if j is not None and i - j <= 65535:
            m = 4
            while i + m < n - 5 and data[j + m] == data[i + m]:
                m += 1
            sequence(data[anchor:i], (i - j, m))
            i += m
            anchor = i
        else:
            i += 1
    sequence(data[anchor:])
    return len(out).to_bytes(4, 'big') + bytes(out)


def sample_text(seed, size):
    rnd = random.Random(seed)
    words = [b'alpha', b'beta', b'gamma', b'delta', b'photo', b'zfs ', b'disk\n', b'0123456789']
    out = bytearray()
    while len(out) < size:
        out += rnd.choice(words) if rnd.random() < 0.9 else rnd.randbytes(rnd.randint(1, 6))
    return bytes(out[:size])


# 'ABC' as literals, a match of 9 bytes from distance 3, then the literal 'D' (a block ends with literals).
_LZ4 = bytes([0x35, 65, 66, 67, 3, 0, 0x10, 68])
LZ4_VECTOR = (len(_LZ4).to_bytes(4, 'big') + _LZ4, 13, b'ABCABCABCABCD')


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

    def test_sha256_word_order(self):
        # FIPS 180-2 example: sha256('abc') = ba7816bf 8f01cfea 414140de 5dae2223 b00361a3 96177a9c b410ff61 f20015ad;
        # ZFS keeps it as four 64-bit words, each the big-endian value of 8 digest bytes
        self.assertEqual(zsread.checksum(8, b'abc'), (0xba7816bf8f01cfea, 0x414140de5dae2223,
                                                       0xb00361a396177a9c, 0xb410ff61f20015ad))
        self.assertIsNone(zsread.checksum(6, b'abc'))         # fletcher2: not supported

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

    def test_lz4_python_vector(self):
        src, dlen, want = LZ4_VECTOR
        self.assertEqual(zsread.lz4(src, dlen), want)
        self.assertEqual(zsread.lz4(src + b'\0' * 500, dlen), want)       # sector padding after the block

    def test_lz4_python_roundtrip(self):
        for seed, size in ((1, 100), (2, 3000), (3, 16384), (4, 131072)):
            data = sample_text(seed, size)
            packed = lz4_encode(data)
            self.assertLess(len(packed), len(data))
            self.assertEqual(zsread.lz4(packed, size), data)

    def test_lz4_python_rejects_garbage(self):
        src, dlen, _ = LZ4_VECTOR
        body = src[4:]
        for bad, why in ((src[:3], 'no header'),
                         ((100).to_bytes(4, 'big') + body, 'length beyond the input'),
                         (len(body).to_bytes(4, 'big') + body[:4] + b'\0\0' + body[6:], 'offset 0'),
                         (len(body).to_bytes(4, 'big') + body[:4] + b'\x09\0' + body[6:], 'offset beyond output'),
                         ((5).to_bytes(4, 'big') + body[:5], 'match cut off')):
            with self.assertRaises(zsread.ReadError, msg=why):
                zsread.lz4(bad, dlen)
        with self.assertRaises(zsread.ReadError, msg='output longer than lsize'):
            zsread.lz4(src, dlen - 1)
        with self.assertRaises(zsread.ReadError, msg='output shorter than lsize'):
            zsread.lz4(src, dlen + 1)

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

    def test_lz4_vector(self):
        src, dlen, want = LZ4_VECTOR
        self.assertEqual(c_lz4(src, dlen), want)
        self.assertIsNone(c_lz4(src, dlen - 1))

    def test_lz4_roundtrip(self):
        for seed, size in ((1, 100), (2, 3000), (3, 16384), (4, 131072)):
            data = sample_text(seed, size)
            self.assertEqual(c_lz4(lz4_encode(data), size), data)

    def test_lz4_mutated_streams_agree(self):
        rnd = random.Random(12)
        base = lz4_encode(sample_text(8, 16384))
        accepted = 0
        for _ in range(3000):
            s = bytearray(base)
            kind = rnd.randrange(3)
            if kind == 0:
                del s[rnd.randrange(4, len(s)):]
            elif kind == 1:
                for _ in range(rnd.randint(1, 4)):
                    s[rnd.randrange(4, len(s))] = rnd.randrange(256)
            else:
                s[rnd.randrange(4)] = rnd.randrange(256)        # damage the length header
            c, p = c_lz4(bytes(s), 16384), py_lz4(bytes(s), 16384)
            self.assertEqual(c, p, f'stream {bytes(s[:24]).hex()}...')
            accepted += c is not None
        self.assertGreater(accepted, 100)
        self.assertLess(accepted, 3000)

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
        for v in ('legacy-single', 'modern-single', 'modern-mirror', 'modern-twodatasets', 'modern-sha256'):
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
                if d['objset_block'].startswith('sha256'):
                    continue
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
                if d['objset_block'].startswith('sha256'):
                    continue                                    # zs_read_verify is fletcher4 only
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

    def test_lz4_blocks_of_a_modern_pool(self):
        """Every lz4 block zscan finds in modern-single: C and Python decode it to the same lsize bytes,
        and zscan's fletcher4 matches the C one."""
        if 'modern-single' not in self.dev:
            self.skipTest('no modern-single images')
        out = subprocess.run([fx.ZSCAN, self.dev['modern-single']], capture_output=True, text=True).stdout
        rows = [l.split('\t') for l in out.splitlines() if l.split('\t')[2] == 'lz4']
        self.assertGreaterEqual(len(rows), 50)
        with open(self.dev['modern-single'], 'rb') as f:
            for r in rows:
                f.seek(int(r[0], 16))
                raw = f.read(int(r[3]))
                lsize = int(r[4])
                c, p = c_lz4(raw, lsize), py_lz4(raw, lsize)
                self.assertIsNotNone(c, f'lz4 @{r[0]}')
                self.assertEqual(c, p, f'lz4 @{r[0]}')
                self.assertEqual(c_fletcher4(raw), tuple(int(x, 16) for x in r[5].split(':')), f'zscan @{r[0]}')

    def test_read_verify_lz4(self):
        """The meta-dnode root block of a modern pool: lz4, 128 KiB; checked against the checksum in the
        block pointer that ZFS wrote into the objset block."""
        if 'modern-single' not in self.dev:
            self.skipTest('no modern-single images')
        d, words, comp, off, raw = self.objset_block('modern-single')
        bp = zsread.BP(raw[64:192])
        self.assertEqual((bp.comp, bp.lsize), (15, 131072))
        vdev, boff, asize = bp.dvas[0]
        ck = (ctypes.c_uint64 * 4)(*bp.cksum)
        with open(self.dev['modern-single'], 'rb') as f:
            dst = ctypes.create_string_buffer(bp.lsize)
            self.assertEqual(LIB.zs_read_verify(f.fileno(), LABEL_START + boff, bp.psize, ck, 15, dst, bp.lsize), 0)
            f.seek(LABEL_START + boff)
            block = f.read(bp.psize)
        self.assertEqual(dst.raw, zsread.lz4(block, bp.lsize))
        self.assertEqual(zsread.fletcher4(block), tuple(bp.cksum))

    def test_sha256_blocks(self):
        """modern-sha256: the objset block and the lz4 tree root it points to, both verified by sha256;
        a corrupted copy is refused."""
        if 'modern-sha256' not in self.dev:
            self.skipTest('no modern-sha256 images')
        d, words, comp, off, raw = self.objset_block('modern-sha256')
        self.assertTrue(d['objset_block'].startswith('sha256'))
        self.assertEqual(zsread.checksum(8, raw), words)
        bp = zsread.BP(raw[64:192])
        self.assertEqual((bp.cksum_alg, bp.comp), (8, 15))
        dev = zsread.Dev([self.dev['modern-sha256']])
        data = zsread.read_bp(dev, bp)
        self.assertEqual(len(data), bp.lsize)
        self.assertEqual(zsread.BP(data[:128]).type, 10)        # entries of the tree: dnode block pointers
        tmp = os.path.join(self.dir, 'sha256-damaged.img')
        with open(self.dev['modern-sha256'], 'rb') as f, open(tmp, 'wb') as out:
            out.write(f.read())
        with open(tmp, 'r+b') as f:                             # flip one byte in every copy of the root
            for vdev, boff, asize in bp.dvas:
                f.seek(LABEL_START + boff + 100)
                b = f.read(1)
                f.seek(LABEL_START + boff + 100)
                f.write(bytes([b[0] ^ 1]))
        with self.assertRaises(zsread.ReadError):
            zsread.read_bp(zsread.Dev([tmp]), bp)

    def test_unknown_checksum_algorithm_is_refused(self):
        if 'modern-sha256' not in self.dev:
            self.skipTest('no modern-sha256 images')
        d, words, comp, off, raw = self.objset_block('modern-sha256')
        w = list(struct.unpack('<16Q', raw[64:192]))
        w[6] = (w[6] & ~(0xff << 40)) | (11 << 40)              # sha512
        with self.assertRaises(zsread.ReadError):
            zsread.read_bp(zsread.Dev([self.dev['modern-sha256']]), zsread.BP(struct.pack('<16Q', *w)))

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
