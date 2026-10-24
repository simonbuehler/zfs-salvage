/*
 * libzsalvage – fast helpers for zsread.py (via ctypes):
 *   zs_fletcher4(buf, len, out[4])
 *   zs_lzjb(src, slen, dst, dlen) -> bytes produced or -1
 *   zs_lz4(src, slen, dst, dlen)  -> bytes produced or -1 (src as ZFS stores it: 4-byte big-endian
 *        length of the LZ4 block, then the block)
 *   zs_read_verify(fd, offset, psize, cksum[4], comp, dst, lsize) -> 0 ok, 1 checksum mismatch,
 *        2 read error, 3 decompression failed, 4 compression not supported (off, lzjb, lz4 are)
 * Own implementation from the public ZFS on-disk format (no CDDL code).
 */
#define _GNU_SOURCE
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

void zs_fletcher4(const uint8_t *buf, uint64_t len, uint64_t out[4])
{
	uint64_t a = 0, b = 0, c = 0, d = 0;
	for (uint64_t i = 0; i + 4 <= len; i += 4) {
		uint32_t w;
		memcpy(&w, buf + i, 4);
		a += w; b += a; c += b; d += c;
	}
	out[0] = a; out[1] = b; out[2] = c; out[3] = d;
}

int64_t zs_lzjb(const uint8_t *src, uint64_t slen, uint8_t *dst, uint64_t dlen)
{
	const uint8_t *s = src, *send = src + slen;
	uint8_t *d = dst, *dend = dst + dlen;
	unsigned copymask = 1u << 7, copymap = 0;
	while (d < dend) {
		if ((copymask <<= 1) == (1u << 8)) {
			if (s >= send) return -1;
			copymask = 1;
			copymap = *s++;
		}
		if (copymap & copymask) {
			if (s + 1 >= send) return -1;
			int mlen = (s[0] >> 2) + 3;
			int off = ((s[0] << 8) | s[1]) & 0x3ff;
			s += 2;
			if (off == 0 || d - dst < off) return -1;
			const uint8_t *cpy = d - off;
			while (mlen-- > 0 && d < dend) *d++ = *cpy++;
		} else {
			if (s >= send) return -1;
			*d++ = *s++;
		}
	}
	return (int64_t)(d - dst);
}

/* LZ4 block format (lz4 project's "LZ4 Block Format Description"): sequences of
   token (4 bits literal length, 4 bits match length - 4), literal length extension bytes, literals,
   2-byte little-endian offset, match length extension bytes. The last sequence has literals only. */
int64_t zs_lz4(const uint8_t *src, uint64_t slen, uint8_t *dst, uint64_t dlen)
{
	if (slen < 4) return -1;
	uint64_t clen = (uint64_t)src[0] << 24 | (uint64_t)src[1] << 16 | (uint64_t)src[2] << 8 | src[3];
	if (clen > slen - 4) return -1;
	const uint8_t *ip = src + 4, *iend = ip + clen;
	uint8_t *op = dst, *oend = dst + dlen;
	while (ip < iend) {
		unsigned token = *ip++, b;
		uint64_t lit = token >> 4;
		if (lit == 15)
			do {
				if (ip >= iend) return -1;
				b = *ip++;
				lit += b;
			} while (b == 255);
		if (lit > (uint64_t)(iend - ip) || lit > (uint64_t)(oend - op)) return -1;
		memcpy(op, ip, lit);
		op += lit;
		ip += lit;
		if (ip == iend) break;
		if (iend - ip < 2) return -1;
		uint64_t off = ip[0] | (uint64_t)ip[1] << 8;
		ip += 2;
		if (off == 0 || off > (uint64_t)(op - dst)) return -1;
		uint64_t mlen = (token & 15) + 4;
		if ((token & 15) == 15)
			do {
				if (ip >= iend) return -1;
				b = *ip++;
				mlen += b;
			} while (b == 255);
		if (mlen > (uint64_t)(oend - op)) return -1;
		const uint8_t *cpy = op - off;
		while (mlen--) *op++ = *cpy++;
	}
	return (int64_t)(op - dst);
}

int zs_read_verify(int fd, uint64_t offset, uint64_t psize, const uint64_t ck[4], int comp,
		   uint8_t *dst, uint64_t lsize)
{
	uint8_t *raw = malloc(psize);
	if (!raw) return 2;
	uint64_t got = 0;
	while (got < psize) {
		ssize_t r = pread(fd, raw + got, psize - got, (off_t)(offset + got));
		if (r <= 0) { free(raw); return 2; }
		got += (uint64_t)r;
	}
	uint64_t c[4];
	zs_fletcher4(raw, psize, c);
	if (memcmp(c, ck, sizeof c) != 0) { free(raw); return 1; }
	int rc = 0;
	if ((comp == 1 || comp == 3) && psize != lsize) {          /* on / lzjb */
		if (zs_lzjb(raw, psize, dst, lsize) != (int64_t)lsize) rc = 3;
	} else if (comp == 1 || comp == 2 || comp == 3) {          /* off / uncompressed */
		memcpy(dst, raw, lsize < psize ? lsize : psize);
	} else if (comp == 15) {                                    /* lz4 */
		if (zs_lz4(raw, psize, dst, lsize) != (int64_t)lsize) rc = 3;
	} else {
		rc = 4;
	}
	free(raw);
	return rc;
}
