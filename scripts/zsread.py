#!/usr/bin/env python3
"""zsread - read ZFS objsets without importing the pool (read-only, stdlib only).

Runs on the recovery host. Reads blocks via their DVAs from read-only devices,
verifies every copy against the checksum stored in the block pointer
(fletcher4), decompresses lzjb and walks meta-dnode -> dnodes -> ZAP/SA.
Uses libzsalvage.so (C) for checksum/decompression when available.

Devices: colon-separated list in ZSREAD_DEVS (the pool's member devices, opened read-only;
the workstation scripts take them from the case file). Physical offset = DVA offset +
ZSREAD_LABEL_START (default 4 MiB: vdev labels L0/L1 + boot block).

Commands (a candidate is given as: <kind> <dev_offset_hex> <comp> <psize> <lsize>,
kind = OBJSET or IND (an L6 block, i.e. the root of a meta-dnode tree)):
  probe   <candidate>       follow the tree to the root directory and list it
  tree    <candidate>       full listing as TSV: path, type (f/d/l), size, mtime, obj, status
  batch                     candidates on stdin (+ birth), one JSON result line each
  extract <candidate>       'obj<TAB>path' lines on stdin -> tar stream on stdout,
                            manifest lines on stderr
"""
import os
import struct
import sys
import time

DEVS = [d for d in os.environ.get('ZSREAD_DEVS', os.environ.get('ZFSREC_DEVS', '')).split(':') if d]
LABEL_START = int(os.environ.get('ZSREAD_LABEL_START', 0x400000))

DMU_OT_DNODE = 10
DMU_OT_ZNODE = 17
DMU_OT_SA = 44


class ReadError(Exception):
    pass


# ---------------------------------------------------------------- basics
def fletcher4(data):
    a = b = c = d = 0
    m = (1 << 64) - 1
    for (w,) in struct.iter_unpack('<I', data):
        a = (a + w) & m
        b = (b + a) & m
        c = (c + b) & m
        d = (d + c) & m
    return (a, b, c, d)


def lzjb(src, dlen):
    """Pure-Python lzjb decompressor (fallback when libzsalvage.so is missing)."""
    dst = bytearray()
    i = 0
    copymask = 1 << 7
    copymap = 0
    n = len(src)
    while len(dst) < dlen:
        copymask <<= 1
        if copymask == 1 << 8:
            if i >= n:
                raise ReadError('lzjb: input too short')
            copymask = 1
            copymap = src[i]
            i += 1
        if copymap & copymask:
            if i + 1 >= n:
                raise ReadError('lzjb: input too short')
            mlen = (src[i] >> 2) + 3
            off = ((src[i] << 8) | src[i + 1]) & 0x3ff
            i += 2
            cpy = len(dst) - off
            if off == 0 or cpy < 0:
                raise ReadError('lzjb: invalid back-reference')
            for _ in range(mlen):
                if len(dst) >= dlen:
                    break
                dst.append(dst[cpy])
                cpy += 1
        else:
            if i >= n:
                raise ReadError('lzjb: input too short')
            dst.append(src[i])
            i += 1
    return bytes(dst)


class Dev:
    def __init__(self, paths=DEVS):
        self.fds = [os.open(p, os.O_RDONLY) for p in paths if os.path.exists(p)]
        if not self.fds:
            sys.exit('zsread: no readable device – set ZSREAD_DEVS=/dev/...:/dev/... (opened read-only)')
        self.stats = {'ok': 0, 'bad_copy': 0, 'fail': 0}

    def pread(self, idx, off, size):
        return os.pread(self.fds[idx], size, off)


class BP:
    """blkptr_t (128 bytes)."""
    __slots__ = ('w', 'dvas', 'lsize', 'psize', 'comp', 'cksum_alg', 'type', 'level',
                 'birth', 'fill', 'cksum', 'embedded', 'gang', 'hole')

    def __init__(self, raw):
        w = struct.unpack('<16Q', raw)
        self.w = w
        self.hole = not any(w)
        prop = w[6]
        self.embedded = bool((prop >> 39) & 1)
        self.lsize = ((prop & 0xffff) + 1) << 9
        self.psize = (((prop >> 16) & 0xffff) + 1) << 9
        self.comp = (prop >> 32) & 0x7f
        self.cksum_alg = (prop >> 40) & 0xff
        self.type = (prop >> 48) & 0xff
        self.level = (prop >> 56) & 0x1f
        self.birth = w[10]
        self.fill = w[11]
        self.cksum = tuple(w[12:16])
        self.dvas = []
        self.gang = False
        for k in range(3):
            w0, w1 = w[2 * k], w[2 * k + 1]
            asize = (w0 & 0xffffff) << 9
            if asize:
                self.dvas.append(((w0 >> 32), (w1 & ((1 << 63) - 1)) << 9, asize))
                self.gang |= bool(w1 >> 63)

    def __repr__(self):
        d = ' '.join(f'<{v}:{o:x}:{a:x}>' for v, o, a in self.dvas)
        return (f'{d} L{self.level} type={self.type} {self.lsize:x}L/{self.psize:x}P comp={self.comp} '
                f'birth={self.birth} fill={self.fill}')


def _load_lib():
    import ctypes
    for p in (os.path.join(os.path.dirname(os.path.abspath(__file__)), 'libzsalvage.so'),
              '/usr/local/lib/libzsalvage.so'):
        if os.path.exists(p):
            lib = ctypes.CDLL(p)
            lib.zs_read_verify.argtypes = [ctypes.c_int, ctypes.c_uint64, ctypes.c_uint64,
                                           ctypes.POINTER(ctypes.c_uint64), ctypes.c_int,
                                           ctypes.c_char_p, ctypes.c_uint64]
            return lib, ctypes
    return None, None


LIB, CT = _load_lib()


def read_bp(dev, bp):
    """Read and verify a block; returns its lsize decompressed bytes.

    Tries every DVA on every device until one copy matches the checksum.
    """
    if bp.hole:
        return bytes(bp.lsize)
    if bp.embedded:
        raise ReadError('embedded block pointer not supported')
    if bp.gang:
        raise ReadError('gang block not supported')
    if LIB is not None and bp.cksum_alg in (7, 1, 2):
        ck = (CT.c_uint64 * 4)(*bp.cksum)
        buf = CT.create_string_buffer(bp.lsize)
        errors = []
        for vdev, off, asize in bp.dvas:
            for di in range(len(dev.fds)):
                rc = LIB.zs_read_verify(dev.fds[di], LABEL_START + off, bp.psize, ck, bp.comp, buf, bp.lsize)
                if rc == 0:
                    dev.stats['ok'] += 1
                    return buf.raw
                if rc == 1:
                    dev.stats['bad_copy'] += 1
                errors.append(f'rc{rc}@{off:x}/{di}')
        dev.stats['fail'] += 1
        raise ReadError('no valid copy: ' + ','.join(errors))
    errors = []
    for vdev, off, asize in bp.dvas:
        for di in range(len(dev.fds)):
            raw = dev.pread(di, LABEL_START + off, bp.psize)
            if len(raw) != bp.psize:
                errors.append(f'short@{off:x}/{di}')
                continue
            if bp.cksum_alg in (7, 1, 2) and fletcher4(raw) != bp.cksum:
                dev.stats['bad_copy'] += 1
                errors.append(f'cksum@{off:x}/{di}')
                continue
            dev.stats['ok'] += 1
            if bp.comp in (1, 3) and bp.psize != bp.lsize:
                return lzjb(raw, bp.lsize)
            if bp.comp in (1, 2, 3):
                return raw
            raise ReadError(f'compression {bp.comp} not supported')
    dev.stats['fail'] += 1
    raise ReadError('no valid copy: ' + ','.join(errors))


def read_raw_candidate(dev, off, comp, psize, lsize, disk=0):
    raw = dev.pread(disk, off, psize)
    return lzjb(raw, lsize) if comp == 'lzjb' else raw[:lsize]


# ---------------------------------------------------------------- dnodes
class Dnode:
    """dnode_phys_t (512-byte dnodes only; large_dnode is not supported yet)."""

    def __init__(self, raw):
        (self.type, self.indblkshift, self.nlevels, self.nblkptr, self.bonustype,
         self.checksum, self.compress, self.flags) = struct.unpack_from('<8B', raw, 0)
        self.datablkszsec, self.bonuslen = struct.unpack_from('<HH', raw, 8)
        self.maxblkid, self.used = struct.unpack_from('<QQ', raw, 16)
        self.bps = [BP(raw[64 + 128 * i:64 + 128 * (i + 1)]) for i in range(max(self.nblkptr, 1))]
        bo = 64 + 128 * max(self.nblkptr, 1)
        self.bonus = raw[bo:bo + self.bonuslen]
        self.blksz = self.datablkszsec * 512

    def l0_bp(self, dev, blkid):
        """Block pointer of data block blkid (None = hole); indirect blocks are cached."""
        epbs = self.indblkshift - 7
        lvl = self.nlevels - 1
        idx = blkid >> (epbs * lvl)
        if idx >= len(self.bps):
            return None
        bp = self.bps[idx]
        cache = self.__dict__.setdefault('_icache', {})
        while lvl > 0:
            if bp.hole:
                return None
            key = (lvl, blkid >> (epbs * lvl))
            data = cache.get(key)
            if data is None:
                data = read_bp(dev, bp)
                if len(cache) > 64:
                    cache.clear()
                cache[key] = data
            lvl -= 1
            i = (blkid >> (epbs * lvl)) & ((1 << epbs) - 1)
            bp = BP(data[128 * i:128 * (i + 1)])
        return None if bp.hole else bp

    def read_block(self, dev, blkid):
        bp = self.l0_bp(dev, blkid)
        return bytes(self.blksz) if bp is None else read_bp(dev, bp)


class Objset:
    """A dataset, given by its meta-dnode (from an objset block or an L6 block)."""

    def __init__(self, dev, meta):
        self.dev = dev
        self.meta = meta
        self.cache = {}

    def dnode(self, obj):
        blkid, slot = divmod(obj, 32)
        if blkid not in self.cache:
            if len(self.cache) > 512:
                self.cache.clear()
            self.cache[blkid] = self.meta.read_block(self.dev, blkid)
        return Dnode(self.cache[blkid][512 * slot:512 * (slot + 1)])


def meta_from_objset_block(data):
    return Dnode(data[:512])


def meta_from_l6_block(bp_raw):
    """Synthetic 7-level meta-dnode whose only block pointer points to the given L6 block."""
    dn = bytearray(512)
    struct.pack_into('<8B', dn, 0, DMU_OT_DNODE, 14, 7, 1, 0, 7, 3, 0)
    struct.pack_into('<HH', dn, 8, 32, 0)
    struct.pack_into('<Q', dn, 16, (1 << 40))
    dn[64:192] = bp_raw
    return Dnode(bytes(dn))


# ---------------------------------------------------------------- ZAP
ZBT_LEAF = (1 << 63) + 0
ZBT_HEADER = (1 << 63) + 1
ZBT_MICRO = (1 << 63) + 3


def zap_read(os_, obj):
    """Return all entries of a micro or fat ZAP object as {name: value}."""
    dn = os_.dnode(obj)
    blk0 = dn.read_block(os_.dev, 0)
    btype, = struct.unpack_from('<Q', blk0, 0)
    res = {}
    if btype == ZBT_MICRO:
        for i in range(1, len(blk0) // 64):
            e = blk0[64 * i:64 * (i + 1)]
            val, = struct.unpack_from('<Q', e, 0)
            name = e[14:64].split(b'\0', 1)[0]
            if name:
                res[name.decode('utf-8', 'surrogateescape')] = val
        return res
    if btype != ZBT_HEADER:
        raise ReadError(f'not a ZAP (block type {btype:x})')
    bs = len(blk0)
    # walk all leaf blocks (1..maxblkid) instead of following the pointer table
    for b in range(1, dn.maxblkid + 1):
        leaf = dn.read_block(os_.dev, b)
        if struct.unpack_from('<Q', leaf, 0)[0] != ZBT_LEAF:
            continue
        nhash = 1 << (bs.bit_length() - 1 - 5)
        cstart = 48 + 2 * nhash
        nchunks = (bs - cstart) // 24

        def chunk(i):
            return leaf[cstart + 24 * i:cstart + 24 * (i + 1)]

        def array(first, nbytes):
            out = bytearray()
            c = first
            while c != 0xffff and len(out) < nbytes and c < nchunks:
                ch = chunk(c)
                out += ch[1:22]
                c, = struct.unpack_from('<H', ch, 22)
            return bytes(out[:nbytes])

        for i in range(nchunks):
            ch = chunk(i)
            if ch[0] != 252:                      # ZAP_CHUNK_ENTRY
                continue
            intlen = ch[1]
            name_chunk, name_numints, val_chunk, val_numints = struct.unpack_from('<HHHH', ch, 4)
            name = array(name_chunk, name_numints).rstrip(b'\0')
            vraw = array(val_chunk, val_numints * intlen)
            if intlen == 8 and val_numints == 1:
                val = struct.unpack('>Q', vraw)[0]
            elif intlen == 2:
                val = list(struct.unpack(f'>{val_numints}H', vraw))
            else:
                val = vraw
            res[name.decode('utf-8', 'surrogateescape')] = val
    return res


# ---------------------------------------------------------------- ZPL / SA
class ZPL:
    def __init__(self, os_):
        self.os = os_
        self.master = zap_read(os_, 1)
        self.root = self.master.get('ROOT')
        self.sa_layouts = {}
        self.sa_attrs = {}
        if 'SA_ATTRS' in self.master:
            sa = zap_read(os_, self.master['SA_ATTRS'])
            reg = zap_read(os_, sa['REGISTRY']) if 'REGISTRY' in sa else {}
            for name, v in reg.items():
                self.sa_attrs[v & 0xffff] = (name, (v >> 24) & 0xffff)
            lay = zap_read(os_, sa['LAYOUTS']) if 'LAYOUTS' in sa else {}
            for k, v in lay.items():
                self.sa_layouts[int(k)] = v if isinstance(v, list) else []

    def attrs(self, dn):
        """dict with size, mtime, mode, parent, links, uid, gid, symlink (SA or legacy znode_phys)."""
        b = dn.bonus
        if dn.bonustype == DMU_OT_SA and len(b) >= 8:
            magic, info = struct.unpack_from('<IH', b, 0)
            if magic != 0x2F505A:
                return {}
            layout = info & 0x3ff
            hdrsz = ((info >> 10) & 0x3f) * 8
            out = {}
            pos = hdrsz
            varidx = 0
            for aid in self.sa_layouts.get(layout, []):
                name, ln = self.sa_attrs.get(aid, (f'attr{aid}', 0))
                if ln == 0:                                   # variable length
                    ln, = struct.unpack_from('<H', b, 6 + 2 * varidx)
                    varidx += 1
                val = b[pos:pos + ln]
                pos += ln
                if name in ('ZPL_SIZE', 'ZPL_MODE', 'ZPL_PARENT', 'ZPL_LINKS', 'ZPL_UID', 'ZPL_GID') and ln >= 8:
                    out[name[4:].lower()] = struct.unpack_from('<Q', val)[0]
                elif name == 'ZPL_MTIME' and ln >= 16:
                    out['mtime'] = struct.unpack_from('<Q', val)[0]
                elif name == 'ZPL_SYMLINK':
                    out['symlink'] = val.rstrip(b'\0').decode('utf-8', 'surrogateescape')
            return out
        if dn.bonustype == DMU_OT_ZNODE and len(b) >= 0x58:
            mtime = struct.unpack_from('<Q', b, 16)[0]
            gen, mode, size, parent, links = struct.unpack_from('<5Q', b, 0x40)
            return {'size': size, 'mode': mode, 'parent': parent, 'mtime': mtime, 'links': links}
        return {}

    def listdir(self, obj):
        """{name: (object id, dirent type)} of a directory."""
        ents = zap_read(self.os, obj)
        return {n: (v & ((1 << 48) - 1), v >> 60) for n, v in ents.items() if isinstance(v, int)}


# ---------------------------------------------------------------- commands
def open_candidate(dev, kind, off, comp, psize, lsize):
    data = read_raw_candidate(dev, off, comp, psize, lsize)
    if kind == 'OBJSET':
        return Objset(dev, meta_from_objset_block(data))
    if kind == 'IND':
        # synthetic block pointer to the L6 block at this offset (checksum = its own fletcher4)
        bp = bytearray(128)
        struct.pack_into('<QQ', bp, 0, psize // 512, (off - LABEL_START) // 512)
        prop = ((lsize // 512 - 1) | ((psize // 512 - 1) << 16) | ((3 if comp == 'lzjb' else 2) << 32)
                | (7 << 40) | (DMU_OT_DNODE << 48) | (6 << 56))
        struct.pack_into('<Q', bp, 48, prop)
        struct.pack_into('<Q', bp, 80, 1)
        struct.pack_into('<4Q', bp, 96, *fletcher4(dev.pread(0, off, psize)))
        return Objset(dev, meta_from_l6_block(bytes(bp)))
    raise SystemExit(f'unknown candidate kind {kind}')


def parse_candidate(args):
    return args[0], int(args[1], 16), args[2], int(args[3]), int(args[4])


def fmt_time(t):
    return time.strftime('%Y-%m-%d %H:%M', time.gmtime(t)) if t else '-'


def cmd_probe(args):
    dev = Dev()
    os_ = open_candidate(dev, *parse_candidate(args))
    print(f'meta-dnode: nlevels={os_.meta.nlevels} bp0={os_.meta.bps[0]}')
    z = ZPL(os_)
    print(f'master node: {dict(z.master)}')
    ents = z.listdir(z.root)
    print(f'root directory (obj {z.root}): {len(ents)} entries')
    for n, (o, t) in sorted(ents.items())[:60]:
        try:
            a = z.attrs(os_.dnode(o))
        except ReadError as e:
            a = {'error': str(e)}
        print(f'  {n:40} obj={o:<8} type={t:<2} size={a.get("size", "?"):<12} mtime={fmt_time(a.get("mtime"))}')
    print('read stats:', dev.stats)


def cmd_tree(args):
    dev = Dev()
    os_ = open_candidate(dev, *parse_candidate(args))
    z = ZPL(os_)
    stack = [('', z.root)]
    seen = set()
    n = 0
    while stack:
        path, obj = stack.pop()
        if obj in seen:
            continue
        seen.add(obj)
        try:
            ents = z.listdir(obj)
        except ReadError as e:
            print(f'{path}/\tDIR_ERROR\t\t\t{obj}\t{e}')
            continue
        for name, (o, t) in sorted(ents.items()):
            p = f'{path}/{name}'
            try:
                a = z.attrs(os_.dnode(o))
                st = 'ok'
            except ReadError as e:
                a, st = {}, f'DNODE_ERROR {e}'
            kindc = 'd' if t == 4 else ('l' if t == 10 else 'f')
            print(f'{p}\t{kindc}\t{a.get("size", "")}\t{fmt_time(a.get("mtime"))}\t{o}\t{st}')
            n += 1
            if t == 4:
                stack.append((p, o))
    print(f'# {n} entries, read stats {dev.stats}', file=sys.stderr)


def cmd_batch(args):
    """Candidates on stdin (kind off comp psize lsize birth), one JSON line per candidate."""
    import json
    import signal

    class Timeout(Exception):
        pass

    def on_alarm(*_):
        raise Timeout()

    signal.signal(signal.SIGALRM, on_alarm)
    dev = Dev()
    for line in sys.stdin:
        f = line.split()
        if len(f) < 6:
            continue
        kind, off, comp, psize, lsize, birth = f[0], int(f[1], 16), f[2], int(f[3]), int(f[4]), int(f[5])
        res = {'offset': f'{off:x}', 'kind': kind, 'birth': birth}
        signal.alarm(30)
        t0 = time.time()
        try:
            os_ = open_candidate(dev, kind, off, comp, psize, lsize)
            if kind == 'IND':
                data = read_raw_candidate(dev, off, comp, psize, lsize)
                res['objects'] = sum(BP(data[128 * i:128 * (i + 1)]).fill for i in range(128))
            else:
                res['objects'] = os_.meta.bps[0].fill
            z = ZPL(os_)
            names = sorted(z.listdir(z.root).keys())
            res['root'] = names[:500]       # the target is recognised by these names
            res['n_root'] = len(names)
            res['ok'] = True
        except Timeout:
            res.update(ok=False, error='timeout')
        except Exception as e:  # noqa: BLE001 - every failure is a finding
            res.update(ok=False, error=f'{type(e).__name__}: {e}'[:200])
        finally:
            signal.alarm(0)
        res['secs'] = round(time.time() - t0, 2)
        print(json.dumps(res, ensure_ascii=False), flush=True)


class FileStream:
    """A ZPL file read block by block (verified) as a file object; tracks SHA-256 and bad blocks."""

    def __init__(self, os_, dn, size):
        import hashlib
        self.os, self.dn, self.size = os_, dn, size
        self.blk = 0
        self.nblk = (size + dn.blksz - 1) // dn.blksz if dn.blksz else 0
        self.buf = b''
        self.done = 0
        self.sha = hashlib.sha256()
        self.bad = []

    def read(self, n=-1):
        while (n < 0 or len(self.buf) < n) and self.blk < self.nblk:
            try:
                data = self.dn.read_block(self.os.dev, self.blk)
            except ReadError:
                self.bad.append(self.blk)
                data = bytes(self.dn.blksz)
            self.blk += 1
            data = data[:max(0, self.size - self.done)]
            self.done += len(data)
            self.buf += data
        if n < 0:
            n = len(self.buf)
        out, self.buf = self.buf[:n], self.buf[n:]
        self.sha.update(out)
        return out


def cmd_extract(args):
    """stdin: 'obj<TAB>path' lines. stdout: tar stream.
    stderr: manifest lines 'M<TAB>status<TAB>path<TAB>size<TAB>sha256<TAB>bad_blocks',
    status = OK | PARTIAL | DNODE_ERROR | LINK; a final 'S<TAB>{stats}' line."""
    import json
    import tarfile
    dev = Dev()
    os_ = open_candidate(dev, *parse_candidate(args))
    z = ZPL(os_)
    tar = tarfile.open(fileobj=sys.stdout.buffer, mode='w|', format=tarfile.PAX_FORMAT)
    for line in sys.stdin:
        line = line.rstrip('\n')
        if not line:
            continue
        obj, path = line.split('\t', 1)
        try:
            dn = os_.dnode(int(obj))
            a = z.attrs(dn)
        except ReadError as e:
            print(f'M\tDNODE_ERROR\t{path}\t0\t-\t{e}', file=sys.stderr, flush=True)
            continue
        ti = tarfile.TarInfo(path.lstrip('/'))
        ti.mtime = a.get('mtime', 0)
        ti.mode = a.get('mode', 0o644) & 0o7777
        ti.uid, ti.gid = 0, 0
        fmt = a.get('mode', 0o100000) & 0o170000
        if fmt == 0o120000:                                   # symlink
            ti.type = tarfile.SYMTYPE
            ti.linkname = a.get('symlink') or dn.read_block(dev, 0)[:a.get('size', 0)].decode('utf-8', 'surrogateescape')
            tar.addfile(ti)
            print(f'M\tLINK\t{path}\t0\t-\t-', file=sys.stderr, flush=True)
            continue
        if fmt == 0o040000:                                   # directory
            ti.type = tarfile.DIRTYPE
            tar.addfile(ti)
            continue
        size = a.get('size', 0)
        ti.size = size
        fs = FileStream(os_, dn, size)
        tar.addfile(ti, fs)
        status = 'OK' if not fs.bad else 'PARTIAL'
        bad = ','.join(map(str, fs.bad[:50])) or '-'
        print(f'M\t{status}\t{path}\t{size}\t{fs.sha.hexdigest()}\t{bad}', file=sys.stderr, flush=True)
    tar.close()
    print(f'S\t{json.dumps(dev.stats)}', file=sys.stderr, flush=True)


if __name__ == '__main__':
    cmds = {'probe': cmd_probe, 'tree': cmd_tree, 'batch': cmd_batch, 'extract': cmd_extract}
    if len(sys.argv) < 2 or sys.argv[1] not in cmds:
        print(__doc__)
        sys.exit(2)
    cmds[sys.argv[1]](sys.argv[2:])
