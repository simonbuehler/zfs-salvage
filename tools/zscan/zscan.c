/*
 * zscan – find ZFS metadata blocks on a block device (read-only).
 *
 * Recognises at every 512-byte position (lzjb-compressed or uncompressed):
 *   OBJSET  objset_phys_t of a ZPL dataset (meta-dnode type 10, os_type 2)
 *   IND     indirect block of a dnode tree (128 block pointers of type 10)
 *   DNODES  dnode block (32 slots of dnode_phys_t)
 *
 * Usage: zscan <device> [start_byte] [end_byte]  > finds.tsv
 * Output (TSV): offset_hex kind comp psize lsize fletcher4 details...
 *   offset_hex = byte offset on the device; DVA offset = offset - 0x400000.
 *
 * Current assumptions (see the capability matrix in README.md): lzjb (comp 3) or off,
 * fletcher4 (cksum 7), 16 KiB dnode/indirect blocks, 2 KiB objset, vdev 0 only.
 * Plausibility limit: birth <= MAX_TXG.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <linux/fs.h>
#include <time.h>
#include <unistd.h>

#define SECT 512
#define CHUNK (64u << 20)
#define OVERLAP (32u << 10)          /* largest psize we evaluate */
#define BLK16K 16384
#define OBJSET_LSIZE 2048
#define MAX_TXG 800000ULL

static uint64_t devsize;

/* ---------- LZJB (as in module/zfs/lzjb.c), bails out on garbage ---------- */
/* returns bytes produced; *used = input bytes consumed; -1 on error */
static int lzjb_dec(const uint8_t *src, size_t slen, uint8_t *dst, size_t want, size_t *used)
{
	const uint8_t *s = src, *send = src + slen;
	uint8_t *d = dst, *dend = dst + want;
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
	*used = (size_t)(s - src);
	return (int)(d - dst);
}

static void fletcher4(const uint8_t *buf, size_t len, uint64_t ck[4])
{
	uint64_t a = 0, b = 0, c = 0, d = 0;
	for (size_t i = 0; i + 4 <= len; i += 4) {
		uint32_t w;
		memcpy(&w, buf + i, 4);
		a += w; b += a; c += b; d += c;
	}
	ck[0] = a; ck[1] = b; ck[2] = c; ck[3] = d;
}

/* ---------- Blockpointer ---------- */
typedef struct {
	uint64_t w[16];
} bp_t;

#define BP_LSIZE(p)  ((((p) & 0xffffULL) + 1) << 9)
#define BP_PSIZE(p)  (((((p) >> 16) & 0xffffULL) + 1) << 9)
#define BP_COMP(p)   (((p) >> 32) & 0x7f)
#define BP_EMBED(p)  (((p) >> 39) & 1)
#define BP_CKSUM(p)  (((p) >> 40) & 0xff)
#define BP_TYPE(p)   (((p) >> 48) & 0xff)
#define BP_LEVEL(p)  (((p) >> 56) & 0x1f)

static int bp_zero(const bp_t *bp)
{
	for (int i = 0; i < 16; i++) if (bp->w[i]) return 0;
	return 1;
}

/* plausible block pointer (not embedded) of the expected type (-1 = any) */
static int bp_ok(const bp_t *bp, int type, int level)
{
	uint64_t prop = bp->w[6];
	if (BP_EMBED(prop)) return 0;
	if (type >= 0 && (int)BP_TYPE(prop) != type) return 0;
	if (level >= 0 && (int)BP_LEVEL(prop) != level) return 0;
	if (BP_CKSUM(prop) != 7 && BP_CKSUM(prop) != 8) return 0;          /* fletcher4 / sha256 */
	if (BP_COMP(prop) != 2 && BP_COMP(prop) != 3) return 0;            /* off / lzjb */
	if (BP_LSIZE(prop) > (128u << 10) || BP_PSIZE(prop) > BP_LSIZE(prop)) return 0;
	uint64_t birth = bp->w[10];
	if (birth == 0 || birth > MAX_TXG) return 0;
	int ndva = 0;
	for (int k = 0; k < 3; k++) {
		uint64_t w0 = bp->w[2 * k], w1 = bp->w[2 * k + 1];
		uint64_t asize = (w0 & 0xffffff) << 9;
		if (!asize) { if (w1 && k == 0) return 0; continue; }
		if ((w0 >> 32) != 0) return 0;                                 /* vdev 0 only */
		uint64_t off = (w1 & ~(UINT64_C(1) << 63)) << 9;
		if (off + asize > devsize) return 0;
		ndva++;
	}
	return ndva > 0;
}

/* ---------- dnode_phys_t ---------- */
static int dnode_ok(const uint8_t *dn)
{
	uint8_t type = dn[0], ibs = dn[1], nlev = dn[2], nbp = dn[3];
	uint8_t ck = dn[5], comp = dn[6];
	uint16_t dbs;
	memcpy(&dbs, dn + 8, 2);
	if (!(type <= 54 || (type & 0x80))) return 0;
	if (type == 0) return 0;
	if (ibs < 12 || ibs > 17) return 0;
	if (nlev < 1 || nlev > 7) return 0;
	if (nbp < 1 || nbp > 3) return 0;
	if (ck > 13 || comp > 15) return 0;
	if (dbs == 0 || dbs > 256) return 0;                                /* max 128 KiB */
	return 1;
}

static int all_zero(const uint8_t *p, size_t n)
{
	for (size_t i = 0; i < n; i++) if (p[i]) return 0;
	return 1;
}

/* ---------- classification ---------- */
static uint8_t out[BLK16K];

static void report(uint64_t off, const char *kind, int comp, size_t psize, size_t lsize,
		   const uint8_t *raw, const char *details)
{
	uint64_t ck[4];
	fletcher4(raw, psize, ck);
	printf("%" PRIx64 "\t%s\t%s\t%zu\t%zu\t%016" PRIx64 ":%016" PRIx64 ":%016" PRIx64 ":%016" PRIx64 "\t%s\n",
	       off, kind, comp == 3 ? "lzjb" : "off", psize, lsize, ck[0], ck[1], ck[2], ck[3], details);
}

/* data = decompressed content (lsize bytes) */
static int classify(uint64_t off, const uint8_t *raw, const uint8_t *data, size_t lsize,
		    size_t psize, int comp)
{
	char det[256];

	if (lsize == OBJSET_LSIZE || lsize >= 1024) {
		/* OBJSET: meta-dnode type 10, nblkptr 3, os_type 2 at offset 704 */
		if (lsize >= 712 && data[0] == 10 && dnode_ok(data) && data[3] == 3) {
			uint64_t ost;
			memcpy(&ost, data + 704, 8);
			bp_t bp;
			memcpy(&bp, data + 64, sizeof bp);
			if (ost == 2 && bp_ok(&bp, 10, -1)) {
				snprintf(det, sizeof det, "nlevels=%u\tbp_level=%u\tbp_birth=%" PRIu64
					 "\tbp_dva0=%" PRIx64 "\tbp_fill=%" PRIu64,
					 data[2], (unsigned)BP_LEVEL(bp.w[6]), bp.w[10],
					 (bp.w[1] & ~(UINT64_C(1) << 63)) << 9, bp.w[11]);
				report(off, "OBJSET", comp, psize, OBJSET_LSIZE, raw, det);
				return 1;
			}
		}
	}
	if (lsize != BLK16K) return 0;

	/* IND: 128 block pointers of type 10, all on the same level */
	{
		const bp_t *bps = (const bp_t *)data;
		int n = 0, lvl = -1, bad = 0;
		uint64_t maxb = 0, minb = UINT64_MAX;
		for (int i = 0; i < 128 && !bad; i++) {
			if (bp_zero(&bps[i])) continue;
			int l = (int)BP_LEVEL(bps[i].w[6]);
			if (lvl < 0) lvl = l;
			if (!bp_ok(&bps[i], 10, lvl)) { bad = 1; break; }
			n++;
			if (bps[i].w[10] > maxb) maxb = bps[i].w[10];
			if (bps[i].w[10] < minb) minb = bps[i].w[10];
		}
		if (!bad && n > 0 && lvl >= 0 && lvl <= 5) {
			snprintf(det, sizeof det, "child_level=%d\tentries=%d\tmin_birth=%" PRIu64
				 "\tmax_birth=%" PRIu64 "\tfirst_dva0=%" PRIx64,
				 lvl, n, minb, maxb, (bps[0].w[1] & ~(UINT64_C(1) << 63)) << 9);
			report(off, "IND", comp, psize, BLK16K, raw, det);
			return 1;
		}
	}

	/* DNODES: 32 slots, free (0) or plausible; at least one in use */
	{
		int used = 0, bad = 0, zpl = 0;
		uint64_t maxb = 0;
		for (int i = 0; i < 32; i++) {
			const uint8_t *dn = data + 512 * i;
			if (all_zero(dn, 512)) continue;
			if (dn[0] == 0) continue;                       /* freed slot */
			if (!dnode_ok(dn)) { bad = 1; break; }
			used++;
			if (dn[0] == 19 || dn[0] == 20 || dn[0] == 21) zpl++;
			bp_t bp;
			memcpy(&bp, dn + 64, sizeof bp);
			if (!bp_zero(&bp) && !BP_EMBED(bp.w[6]) && bp.w[10] <= MAX_TXG && bp.w[10] > maxb)
				maxb = bp.w[10];
		}
		if (!bad && used > 0 && zpl > 0) {
			snprintf(det, sizeof det, "used=%d\tzpl=%d\tmax_birth=%" PRIu64, used, zpl, maxb);
			report(off, "DNODES", comp, psize, BLK16K, raw, det);
			return 1;
		}
	}
	return 0;
}

static void try_offset(uint64_t off, const uint8_t *p, size_t avail)
{
	size_t used;
	int n;

	/* empty sectors: neither a valid lzjb stream nor a meaningful uncompressed block */
	if (all_zero(p, 64)) return;

	/* uncompressed: only if the start already looks like a dnode or block pointer */
	if (avail >= BLK16K && (dnode_ok(p) || bp_ok((const bp_t *)p, 10, -1))) {
		if (classify(off, p, p, BLK16K, BLK16K, 2)) return;
		if (classify(off, p, p, OBJSET_LSIZE, OBJSET_LSIZE, 2)) return;
	}

	/* lzjb: 64 bytes as a prefilter first, then the full block */
	n = lzjb_dec(p, avail < 2048 ? avail : 2048, out, 64, &used);
	if (n != 64) return;
	int dnodeish = out[0] && dnode_ok(out);
	int bpish = all_zero(out, 64) || ((((uint64_t *)out)[0] >> 32) == 0 && (out[0] | out[1] | out[2]));
	if (!dnodeish && !bpish) return;

	if (out[0] == 10 && dnodeish) {
		n = lzjb_dec(p, avail, out, OBJSET_LSIZE, &used);
		if (n == OBJSET_LSIZE) {
			size_t psize = (used + SECT - 1) / SECT * SECT;
			if (classify(off, p, out, OBJSET_LSIZE, psize, 3)) return;
		}
	}
	n = lzjb_dec(p, avail, out, BLK16K, &used);
	if (n != BLK16K) return;
	size_t psize = (used + SECT - 1) / SECT * SECT;
	if (psize >= BLK16K) return;                    /* compression would not pay off -> ZFS stores it "off" */
	classify(off, p, out, BLK16K, psize, 3);
}

int main(int argc, char **argv)
{
	if (argc < 2) {
		fprintf(stderr, "usage: %s <device> [start] [end]\n", argv[0]);
		return 2;
	}
	int fd = open(argv[1], O_RDONLY);
	if (fd < 0) { perror(argv[1]); return 1; }
	if (ioctl(fd, BLKGETSIZE64, &devsize) != 0) {
		off_t e = lseek(fd, 0, SEEK_END);
		devsize = (uint64_t)e;
	}
	uint64_t start = argc > 2 ? strtoull(argv[2], NULL, 0) : 0;
	uint64_t end = argc > 3 ? strtoull(argv[3], NULL, 0) : devsize;
	if (end > devsize) end = devsize;
	start &= ~(uint64_t)(SECT - 1);
	posix_fadvise(fd, 0, 0, POSIX_FADV_SEQUENTIAL);

	uint8_t *buf = aligned_alloc(4096, CHUNK + OVERLAP);
	if (!buf) { perror("alloc"); return 1; }
	setvbuf(stdout, NULL, _IOLBF, 0);
	fprintf(stderr, "zscan %s: %" PRIu64 " .. %" PRIu64 " (device %" PRIu64 " bytes)\n",
		argv[1], start, end, devsize);

	time_t t0 = time(NULL), tlast = t0;
	for (uint64_t pos = start; pos < end; pos += CHUNK) {
		size_t want = CHUNK + OVERLAP;
		if (pos + want > devsize) want = devsize - pos;
		size_t got = 0;
		while (got < want) {
			ssize_t r = pread(fd, buf + got, want - got, pos + got);
			if (r < 0) {
				if (errno == EINTR) continue;
				fprintf(stderr, "READ ERROR at %" PRIu64 ": %s – range skipped\n",
					pos + got, strerror(errno));
				memset(buf + got, 0, want - got);
				got = want;
				break;
			}
			if (r == 0) break;
			got += (size_t)r;
		}
		size_t lim = got > OVERLAP ? got - OVERLAP : got;
		if (pos + CHUNK >= end) lim = got;
		if (lim > end - pos) lim = end - pos;
		for (size_t s = 0; s < lim; s += SECT)
			try_offset(pos + s, buf + s, got - s);

		time_t now = time(NULL);
		if (now - tlast >= 30) {
			double done = (double)(pos + CHUNK - start) / (double)(end - start);
			fprintf(stderr, "%.2f%%  %.1f GB  %.0f MB/s\n", 100 * done,
				(pos + CHUNK - start) / 1e9, (pos + CHUNK - start) / 1e6 / (double)(now - t0));
			tlast = now;
		}
	}
	fprintf(stderr, "FERTIG nach %ld s\n", (long)(time(NULL) - t0));
	return 0;
}
