"""Embedded block pointers (feature embedded_data): tiny files and small directories stored inside
the block pointer itself."""
import struct
import unittest

import fx
import zsread
from test_codecs import lz4_encode

LABEL_START = 4 << 20


def embedded_bp(payload, lsize, comp, etype=0, otype=19, level=0, birth=77):
    """Build an embedded block pointer from its payload (up to 112 bytes)."""
    assert len(payload) <= 112
    prop = ((lsize - 1) | ((len(payload) - 1) << 25) | (comp << 32) | (1 << 39) | (etype << 40)
            | (otype << 48) | (level << 56))
    data = payload.ljust(112, b'\0')
    words, k = [0] * 16, 0
    for i in (0, 1, 2, 3, 4, 5, 7, 8, 9, 11, 12, 13, 14, 15):
        words[i] = struct.unpack_from('<Q', data, 8 * k)[0]
        k += 1
    words[6], words[10] = prop, birth
    return zsread.BP(struct.pack('<16Q', *words))


class Synthetic(unittest.TestCase):
    def test_uncompressed_payload(self):
        text = b'tiny file stored as embedded data\n'
        bp = embedded_bp(text, len(text), 2)
        self.assertTrue(bp.embedded)
        self.assertEqual((bp.lsize, bp.psize, bp.birth, bp.dvas), (len(text), len(text), 77, []))
        self.assertEqual(zsread.read_bp(None, bp), text)      # no device access at all

    def test_lz4_payload(self):
        text = b'abcd efgh ' * 30
        packed = lz4_encode(text)
        self.assertLessEqual(len(packed), 112)
        bp = embedded_bp(packed, len(text), 15)
        self.assertEqual(zsread.read_bp(None, bp), text)

    def test_full_112_byte_payload(self):
        data = bytes(range(112))
        self.assertEqual(zsread.read_bp(None, embedded_bp(data, 112, 2)), data)

    def test_other_embedded_types_are_refused(self):
        with self.assertRaises(zsread.ReadError):
            zsread.read_bp(None, embedded_bp(b'x' * 10, 10, 2, etype=2))

    def test_unknown_compression_is_refused(self):
        with self.assertRaises(zsread.ReadError):
            zsread.read_bp(None, embedded_bp(b'x' * 10, 40, 99))


@unittest.skipUnless(fx.have_images('modern-single'), 'needs the modern-single images')
class RealPool(unittest.TestCase):
    """In modern-single (ZFS 2.2) the SA attribute registry, directories with one entry and docs/tiny.txt
    are embedded; without them no file attribute and no path could be read."""

    @classmethod
    def setUpClass(cls):
        cls.dir = fx.tmpdir()
        cls.dev = zsread.Dev(fx.unpack('modern-single', cls.dir))
        s = fx.sidecars()['modern-single']
        tg = s['datasets'][s['target']]
        os_ = zsread.open_candidate(cls.dev, 'OBJSET', LABEL_START + tg['dvas'][0]['offset'], 'off',
                                    tg['psize'], tg['lsize'])
        cls.zpl = zsread.ZPL(os_)
        cls.os = os_
        cls.files = {f['path']: f for f in s['files']}

    @classmethod
    def tearDownClass(cls):
        fx.rmtree(cls.dir)

    def entry(self, path):
        obj = self.zpl.root
        for name in path.strip('/').split('/'):
            obj = self.zpl.listdir(obj)[name][0]
        return self.os.dnode(obj)

    def test_sa_registry_is_embedded(self):
        dn = self.os.dnode(self.zpl.master['SA_ATTRS'])
        self.assertTrue(dn.bps[0].embedded)
        self.assertEqual(dn.bps[0].comp, 15)
        self.assertEqual(self.zpl.attrs(self.entry('/docs/note-00.txt'))['size'], self.files['/docs/note-00.txt']['size'])

    def test_small_directory_is_embedded(self):
        dn = self.entry('/a/b')
        self.assertTrue(dn.bps[0].embedded)
        self.assertEqual(set(self.zpl.listdir(self.zpl.listdir(self.zpl.listdir(self.zpl.root)['a'][0])['b'][0])),
                         {'c'})

    def test_tiny_file_is_embedded_and_correct(self):
        dn = self.entry('/docs/tiny.txt')
        self.assertTrue(dn.bps[0].embedded)
        want = self.files['/docs/tiny.txt']
        self.assertEqual(dn.read_block(self.dev, 0)[:want['size']].decode(),
                         'tiny file stored as embedded data in the block pointer\n')
        import hashlib
        self.assertEqual(hashlib.sha256(dn.read_block(self.dev, 0)[:want['size']]).hexdigest(), want['sha256'])


if __name__ == '__main__':
    unittest.main()
