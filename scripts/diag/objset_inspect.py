#!/usr/bin/env python3
"""Analyse raw copies of a ZFS objset_phys block (lzjb, fletcher4).

Usage: python3 -I scripts/diag/objset_inspect.py <expected_cksum a:b:c:d> <lsize> file...

For each file: fletcher4 over the physical bytes (compared with the value from the
block pointer), lzjb decompression to lsize, and a decode of the meta-dnode
(type, levels, block pointers with DVA/birth/checksum). Writes <file>.raw (decompressed).
"""
import struct
import sys

DMU_OT = {10: 'DNODE', 11: 'OBJSET', 0: 'NONE'}


def fletcher4(data):
    a = b = c = d = 0
    m = (1 << 64) - 1
    for (w,) in struct.iter_unpack('<I', data[:len(data) // 4 * 4]):
        a = (a + w) & m
        b = (b + a) & m
        c = (c + b) & m
        d = (d + c) & m
    return a, b, c, d


def lzjb_decompress(src, dlen):
    dst = bytearray()
    i = 0
    copymask = 1 << 7
    copymap = 0
    while len(dst) < dlen:
        copymask <<= 1
        if copymask == 1 << 8:
            if i >= len(src):
                break
            copymask = 1
            copymap = src[i]
            i += 1
        if copymap & copymask:
            if i + 1 >= len(src):
                break
            mlen = (src[i] >> 2) + 3
            offset = ((src[i] << 8) | src[i + 1]) & 0x3ff
            i += 2
            cpy = len(dst) - offset
            if offset == 0 or cpy < 0:
                return dst, f'invalid back reference at input byte {i - 2}, output {len(dst)}'
            for _ in range(mlen):
                if len(dst) >= dlen:
                    break
                dst.append(dst[cpy])
                cpy += 1
        else:
            if i >= len(src):
                break
            dst.append(src[i])
            i += 1
    return dst, 'ok' if len(dst) == dlen else f'only {len(dst)} bytes'


def blkptr(bp):
    w = struct.unpack('<16Q', bp)
    if not any(w):
        return 'leer'
    dvas = []
    for k in range(3):
        w0, w1 = w[2 * k], w[2 * k + 1]
        asize = (w0 & 0xffffff) << 9
        vdev = w0 >> 32
        offset = (w1 & ((1 << 63) - 1)) << 9
        if asize:
            dvas.append(f'<{vdev}:{offset:x}:{asize:x}>')
    prop = w[6]
    lsize = ((prop & 0xffff) + 1) << 9
    psize = (((prop >> 16) & 0xffff) + 1) << 9
    comp = (prop >> 32) & 0x7f
    cks = (prop >> 40) & 0xff
    typ = (prop >> 48) & 0xff
    lvl = (prop >> 56) & 0x1f
    birth = w[10]
    fill = w[11]
    ck = ':'.join(f'{x:016x}' for x in w[12:16])
    return (f'{" ".join(dvas)} L{lvl} type={typ} lsize={lsize:x} psize={psize:x} comp={comp} '
            f'cksum_alg={cks} birth={birth} fill={fill} cksum={ck}')


def main():
    want = tuple(int(x, 16) for x in sys.argv[1].split(':'))
    lsize = int(sys.argv[2], 0)
    for fn in sys.argv[3:]:
        raw = open(fn, 'rb').read()
        got = fletcher4(raw)
        ok = 'PASST' if got == want else 'FALSCH'
        print(f'=== {fn}: fletcher4 {ok}  ({":".join(f"{x:016x}" for x in got)})')
        data, st = lzjb_decompress(raw, lsize)
        print(f'    lzjb: {st}')
        open(fn + '.raw', 'wb').write(bytes(data))
        if len(data) < 512:
            continue
        dn = data[:512]
        dn_type, ibs, nlevels, nblkptr, bonustype, cks, comp = struct.unpack_from('<7B', dn, 0)
        dblkszsec, = struct.unpack_from('<H', dn, 8)
        maxblkid, = struct.unpack_from('<Q', dn, 16)
        print(f'    meta-dnode: type={dn_type}({DMU_OT.get(dn_type, "?")}) indblkshift={ibs} '
              f'nlevels={nlevels} nblkptr={nblkptr} datablksz={dblkszsec * 512} maxblkid={maxblkid}')
        for k in range(min(nblkptr, 3) or 1):
            print(f'    blkptr[{k}]: {blkptr(dn[64 + 128 * k:64 + 128 * (k + 1)])}')
        os_type, = struct.unpack_from('<Q', data, 512 + 192)  # after dnode(512) + zil_header(192)
        print(f'    os_type={os_type} (2=ZFS/ZPL expected)')


if __name__ == '__main__':
    main()
