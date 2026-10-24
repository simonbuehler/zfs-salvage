/*
 * zscan – find ZFS metadata blocks on a block device (read-only).
 *
 * Recognises at every 512-byte position (lz4- or lzjb-compressed, or uncompressed):
 *   OBJSET  objset_phys_t of a ZPL dataset or a zvol (meta-dnode type 10, os_type 2 or 3; reported as os_type=)
 *   IND     indirect block of a dnode tree (128 block pointers of type 10)
 *   DNODES  dnode block (32 slots of dnode_phys_t)
 *
 * Usage: zscan [--max-txg N] [--ind-size N ...] [--max-vdev N] <device> [start_byte] [end_byte]  > finds.tsv
 * Output (TSV): offset_hex kind comp psize lsize fletcher4 details...
 *   offset_hex = byte offset on the device; DVA offset = offset - 0x400000.
 *
 * Current assumptions (see the capability matrix in README.md): lz4 (comp 15), lzjb (comp 3) or off,
 * fletcher4 (cksum 7), 16 KiB dnode blocks, indirect blocks of the sizes given with --ind-size
 * (default 128 KiB and 16 KiB, tried largest first), 2 or 4 KiB objset, DVAs on top-level vdevs
 * 0..max_vdev (--max-vdev N, default 0: one top-level vdev). Offsets are on the scanned device; run
 * zscan once per top-level vdev (one child of a mirror is enough).
 * Plausibility limit: a block pointer's birth TXG must be in 1..max_txg (--max-txg N, default: no
 * practical limit). scripts/scan-device.sh passes the newest uberblock TXG of the case plus a margin.
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
#define OVERLAP (132u << 10)         /* largest psize we evaluate (128 KiB) + margin */
#define BLK16K 16384                  /* dnode blocks */
#define IND_MAX (128u << 10)          /* largest indirect block */
#define OBJSET_V2 2048                /* objset_phys_t without, */
#define OBJSET_V3 4096                /* and with the project-used dnode */

static uint64_t devsize;
static uint64_t max_txg = UINT64_MAX >> 1;      /* --max-txg N */
static size_t ind_sizes[6] = {IND_MAX, BLK16K}; /* --ind-size N (repeatable), largest first */
static int n_ind = 2;
static uint64_t max_vdev = 0;                   /* --max-vdev N */

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

/*
 * Decode up to `want` bytes and stop quietly at the first error. Returns the bytes produced.
 * used_at[k] = input consumed when the output first reached marks[k] (SIZE_MAX if it never did), so
 * one pass answers "is this a valid block of size 16 KiB? of 128 KiB?".
 */
static size_t lzjb_marks(const uint8_t *src, size_t slen, uint8_t *dst, size_t want,
			 const size_t *marks, size_t *used_at, int nmarks)
{
	const uint8_t *s = src, *send = src + slen;
	uint8_t *d = dst, *dend = dst + want;
	unsigned copymask = 1u << 7, copymap = 0;
	int k = 0;

	for (int i = 0; i < nmarks; i++) used_at[i] = SIZE_MAX;
	while (d < dend) {
		if ((copymask <<= 1) == (1u << 8)) {
			if (s >= send) break;
			copymask = 1;
			copymap = *s++;
		}
		if (copymap & copymask) {
			if (s + 1 >= send) break;
			int mlen = (s[0] >> 2) + 3;
			int off = ((s[0] << 8) | s[1]) & 0x3ff;
			if (off == 0 || d - dst < off) break;
			s += 2;
			const uint8_t *cpy = d - off;
			while (mlen-- > 0 && d < dend) *d++ = *cpy++;
		} else {
			if (s >= send) break;
			*d++ = *s++;
		}
		for (; k < nmarks && (size_t)(d - dst) >= marks[k]; k++)
			used_at[k] = (size_t)(s - src);
	}
	return (size_t)(d - dst);
}

/*
 * LZ4 block (lz4 project's "LZ4 Block Format Description"), without ZFS's 4-byte length header.
 * Decodes until the input is used up or at least `stop` bytes were produced (stop <= cap).
 * Returns the bytes produced, or -1 on malformed input or when the output would exceed cap.
 * *done = 1 if the whole input was consumed.
 */
static long lz4_dec(const uint8_t *src, size_t slen, uint8_t *dst, size_t cap, size_t stop, int *done)
{
	const uint8_t *ip = src, *iend = src + slen;
	uint8_t *op = dst, *oend = dst + cap;

	*done = 0;
	while (ip < iend) {
		unsigned token = *ip++, b;
		size_t lit = token >> 4;
		if (lit == 15)
			do {
				if (ip >= iend) return -1;
				b = *ip++;
				lit += b;
			} while (b == 255);
		if (lit > (size_t)(iend - ip) || lit > (size_t)(oend - op)) return -1;
		memcpy(op, ip, lit);
		op += lit;
		ip += lit;
		if (ip == iend) break;
		if ((size_t)(op - dst) >= stop) return (long)(op - dst);
		if (iend - ip < 2) return -1;
		size_t off = ip[0] | (size_t)ip[1] << 8;
		ip += 2;
		if (off == 0 || off > (size_t)(op - dst)) return -1;
		size_t mlen = (token & 15) + 4;
		if ((token & 15) == 15)
			do {
				if (ip >= iend) return -1;
				b = *ip++;
				mlen += b;
			} while (b == 255);
		if (mlen > (size_t)(oend - op)) return -1;
		const uint8_t *cpy = op - off;
		while (mlen--) *op++ = *cpy++;
		if ((size_t)(op - dst) >= stop) return (long)(op - dst);
	}
	*done = 1;
	return (long)(op - dst);
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
	if (BP_COMP(prop) != 2 && BP_COMP(prop) != 3 && BP_COMP(prop) != 15) return 0;   /* off/lzjb/lz4 */
	if (BP_LSIZE(prop) > (128u << 10) || BP_PSIZE(prop) > BP_LSIZE(prop)) return 0;
	uint64_t birth = bp->w[10];
	if (birth == 0 || birth > max_txg) return 0;
	int ndva = 0;
	for (int k = 0; k < 3; k++) {
		uint64_t w0 = bp->w[2 * k], w1 = bp->w[2 * k + 1];
		uint64_t asize = (w0 & 0xffffff) << 9;
		if (!asize) { if (w1 && k == 0) return 0; continue; }
		if ((w0 >> 32) > max_vdev) return 0;                           /* no such top-level vdev */
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
static uint8_t out[IND_MAX];

static void report(uint64_t off, const char *kind, int comp, size_t psize, size_t lsize,
		   const uint8_t *raw, const char *details)
{
	uint64_t ck[4];
	fletcher4(raw, psize, ck);
	printf("%" PRIx64 "\t%s\t%s\t%zu\t%zu\t%016" PRIx64 ":%016" PRIx64 ":%016" PRIx64 ":%016" PRIx64 "\t%s\n",
	       off, kind, comp == 3 ? "lzjb" : comp == 15 ? "lz4" : "off", psize, lsize, ck[0], ck[1], ck[2], ck[3],
	       details);
}

static size_t roundup_sect(size_t n)
{
	return (n + SECT - 1) / SECT * SECT;
}

/*
 * ZFS keeps a block compressed only if that saves at least 1/8 of its size; otherwise it is stored
 * uncompressed. So `used` input bytes can only be a block of `lsize` bytes if used <= 7/8 lsize. This
 * also rejects a smaller block followed by zeros: lzjb reads zeros as literals (9 bytes in, 8 out).
 */
static int compressed_fits(size_t used, size_t lsize)
{
	return used != SIZE_MAX && used <= lsize - lsize / 8;
}

/* objset_phys_t is 2 KiB on older pools, 4 KiB with the project-used dnode; a 4 KiB one is zero
   from byte 2560 on (padding after the third accounting dnode) */
static size_t objset_lsize(const uint8_t *data, size_t have)
{
	return have >= OBJSET_V3 && all_zero(data + 2560, OBJSET_V3 - 2560) ? OBJSET_V3 : OBJSET_V2;
}

/* OBJSET: meta-dnode type 10, nblkptr 3, os_type 2 (file system) or 3 (zvol) at offset 704; data holds lsize bytes */
static int classify_objset(uint64_t off, const uint8_t *raw, const uint8_t *data, size_t lsize,
			   size_t psize, int comp)
{
	char det[256];
	uint64_t ost;
	bp_t bp;

	if (!(data[0] == 10 && dnode_ok(data) && data[3] == 3)) return 0;
	memcpy(&ost, data + 704, 8);
	memcpy(&bp, data + 64, sizeof bp);
	if ((ost != 2 && ost != 3) || !bp_ok(&bp, 10, -1)) return 0;
	snprintf(det, sizeof det, "nlevels=%u\tbp_level=%u\tbp_birth=%" PRIu64
		 "\tbp_dva0=%" PRIx64 "\tbp_fill=%" PRIu64 "\tos_type=%u",
		 data[2], (unsigned)BP_LEVEL(bp.w[6]), bp.w[10],
		 (bp.w[1] & ~(UINT64_C(1) << 63)) << 9, bp.w[11], (unsigned)ost);
	report(off, "OBJSET", comp, psize, lsize, raw, det);
	return 1;
}

/* IND: lsize / 128 block pointers of type 10, all on the same level, at least one in use */
static int classify_ind(uint64_t off, const uint8_t *raw, const uint8_t *data, size_t lsize,
			size_t psize, int comp)
{
	char det[256];
	const bp_t *bps = (const bp_t *)data;
	int n = 0, lvl = -1;
	uint64_t maxb = 0, minb = UINT64_MAX;

	for (size_t i = 0; i < lsize / sizeof(bp_t); i++) {
		if (bp_zero(&bps[i])) continue;
		int l = (int)BP_LEVEL(bps[i].w[6]);
		if (lvl < 0) lvl = l;
		if (!bp_ok(&bps[i], 10, lvl)) return 0;
		n++;
		if (bps[i].w[10] > maxb) maxb = bps[i].w[10];
		if (bps[i].w[10] < minb) minb = bps[i].w[10];
	}
	if (n == 0 || lvl < 0 || lvl > 5) return 0;
	snprintf(det, sizeof det, "child_level=%d\tentries=%d\tmin_birth=%" PRIu64
		 "\tmax_birth=%" PRIu64 "\tfirst_dva0=%" PRIx64,
		 lvl, n, minb, maxb, (bps[0].w[1] & ~(UINT64_C(1) << 63)) << 9);
	report(off, "IND", comp, psize, lsize, raw, det);
	return 1;
}

/* DNODES: a 16 KiB block of 32 slots, free (0) or plausible; at least one ZPL dnode */
static int classify_dnodes(uint64_t off, const uint8_t *raw, const uint8_t *data, size_t psize, int comp)
{
	char det[256];
	int used = 0, zpl = 0;
	uint64_t maxb = 0;

	for (int i = 0; i < 32; i++) {
		const uint8_t *dn = data + 512 * i;
		if (all_zero(dn, 512)) continue;
		if (dn[0] == 0) continue;                       /* freed slot */
		if (!dnode_ok(dn)) return 0;
		used++;
		if (dn[0] == 19 || dn[0] == 20 || dn[0] == 21) zpl++;
		bp_t bp;
		memcpy(&bp, dn + 64, sizeof bp);
		if (!bp_zero(&bp) && !BP_EMBED(bp.w[6]) && bp.w[10] <= max_txg && bp.w[10] > maxb)
			maxb = bp.w[10];
	}
	if (used == 0 || zpl == 0) return 0;
	snprintf(det, sizeof det, "used=%d\tzpl=%d\tmax_birth=%" PRIu64, used, zpl, maxb);
	report(off, "DNODES", comp, psize, BLK16K, raw, det);
	return 1;
}

static void try_offset(uint64_t off, const uint8_t *p, size_t avail)
{
	size_t used;
	int n;

	/* empty sectors: neither a valid lzjb stream nor a meaningful uncompressed block */
	if (all_zero(p, 64)) return;

	/* uncompressed: only if the start already looks like a dnode or block pointer */
	if (dnode_ok(p) || bp_ok((const bp_t *)p, 10, -1)) {
		if (avail >= OBJSET_V2) {
			size_t l = objset_lsize(p, avail);
			if (classify_objset(off, p, p, l, l, 2)) return;
		}
		for (int i = 0; i < n_ind; i++)
			if (avail >= ind_sizes[i] && classify_ind(off, p, p, ind_sizes[i], ind_sizes[i], 2)) return;
		if (avail >= BLK16K && classify_dnodes(off, p, p, BLK16K, 2)) return;
	}

	/* lz4: ZFS stores a 4-byte big-endian length of the LZ4 block in front of it. The decoded length
	   is the block's logical size, so no guessing as with lzjb. */
	{
		size_t clen = (size_t)p[0] << 24 | (size_t)p[1] << 16 | (size_t)p[2] << 8 | p[3];
		if (clen >= 8 && clen + 4 <= avail && compressed_fits(clen + 4, IND_MAX)) {
			int done;
			long got = lz4_dec(p + 4, clen, out, IND_MAX, 64, &done);
			if (got >= 64 && ((out[0] && dnode_ok(out)) || all_zero(out, 64) ||
					  ((((uint64_t *)out)[0] >> 32) == 0 && (out[0] | out[1] | out[2])))) {
				got = lz4_dec(p + 4, clen, out, IND_MAX, IND_MAX, &done);
				size_t l = got > 0 ? (size_t)got : 0, ps = roundup_sect(clen + 4);
				if (done && compressed_fits(clen + 4, l)) {
					if ((l == OBJSET_V2 || l == OBJSET_V3) && classify_objset(off, p, out, l, ps, 15)) return;
					for (int i = 0; i < n_ind; i++)
						if (l == ind_sizes[i] && classify_ind(off, p, out, l, ps, 15)) return;
					if (l == BLK16K && classify_dnodes(off, p, out, ps, 15)) return;
				}
			}
		}
	}

	/* lzjb: 64 bytes as a prefilter first, then the full block */
	n = lzjb_dec(p, avail < 2048 ? avail : 2048, out, 64, &used);
	if (n != 64) return;
	int dnodeish = out[0] && dnode_ok(out);
	int bpish = all_zero(out, 64) || ((((uint64_t *)out)[0] >> 32) == 0 && (out[0] | out[1] | out[2]));
	if (!dnodeish && !bpish) return;

	if (out[0] == 10 && dnodeish) {
		size_t marks[2] = {OBJSET_V2, OBJSET_V3}, at[2];
		size_t got = lzjb_marks(p, avail, out, OBJSET_V3, marks, at, 2);
		/* 4 KiB only if the second half is itself compressed data (a 2 KiB block followed by zeros
		   would decode to zero padding, too) */
		int v3 = got >= OBJSET_V3 && objset_lsize(out, got) == OBJSET_V3 &&
			 compressed_fits(at[1], OBJSET_V3) &&
			 compressed_fits(at[1] - at[0], OBJSET_V3 - OBJSET_V2);
		size_t l = v3 ? OBJSET_V3 : OBJSET_V2;
		if (got >= l && compressed_fits(at[v3], l) && classify_objset(off, p, out, l, roundup_sect(at[v3]), 3))
			return;
	}

	/* one pass to the largest size of interest; every indirect size, then 16 KiB for dnodes */
	size_t marks[7], at[7];
	int nm = 0;
	for (int i = n_ind - 1; i >= 0; i--)                    /* ascending order for lzjb_marks */
		if (ind_sizes[i] != BLK16K) marks[nm++] = ind_sizes[i];
	marks[nm++] = BLK16K;
	for (int a = 0; a < nm; a++)                            /* sort ascending (tiny) */
		for (int b = a + 1; b < nm; b++)
			if (marks[b] < marks[a]) { size_t t = marks[a]; marks[a] = marks[b]; marks[b] = t; }
	size_t got = lzjb_marks(p, avail, out, marks[nm - 1], marks, at, nm);
	for (int i = 0; i < n_ind; i++) {                       /* largest first */
		for (int m = 0; m < nm; m++) {
			if (marks[m] != ind_sizes[i]) continue;
			if (got < marks[m] || !compressed_fits(at[m], marks[m])) break;
			if (classify_ind(off, p, out, marks[m], roundup_sect(at[m]), 3)) return;
		}
	}
	for (int m = 0; m < nm; m++)
		if (marks[m] == BLK16K && got >= BLK16K && compressed_fits(at[m], BLK16K))
			classify_dnodes(off, p, out, roundup_sect(at[m]), 3);
}

int main(int argc, char **argv)
{
	int ai = 1;                                     /* options first, then <device> [start] [end] */
	while (ai < argc && strncmp(argv[ai], "--", 2) == 0) {
		if (strcmp(argv[ai], "--max-txg") == 0 && ai + 1 < argc) {
			char *e;
			max_txg = strtoull(argv[ai + 1], &e, 0);
			if (*argv[ai + 1] == '\0' || *e != '\0') {
				fprintf(stderr, "bad value for --max-txg: %s\n", argv[ai + 1]);
				return 2;
			}
			ai += 2;
		} else if (strcmp(argv[ai], "--max-vdev") == 0 && ai + 1 < argc) {
			char *e;
			max_vdev = strtoull(argv[ai + 1], &e, 0);
			if (*argv[ai + 1] == '\0' || *e != '\0' || max_vdev > 255) {
				fprintf(stderr, "bad value for --max-vdev (0..255): %s\n", argv[ai + 1]);
				return 2;
			}
			ai += 2;
		} else if (strcmp(argv[ai], "--ind-size") == 0 && ai + 1 < argc) {
			static int given;
			char *e;
			unsigned long long v = strtoull(argv[ai + 1], &e, 0);
			if (*argv[ai + 1] == '\0' || *e != '\0' || v < 4096 || v > IND_MAX || (v & (v - 1))) {
				fprintf(stderr, "bad value for --ind-size (power of two, 4096..131072): %s\n", argv[ai + 1]);
				return 2;
			}
			if (!given++) n_ind = 0;                 /* the first --ind-size replaces the default list */
			if (n_ind < 6) ind_sizes[n_ind++] = (size_t)v;
			for (int a = 0; a < n_ind; a++)          /* keep them largest first */
				for (int b = a + 1; b < n_ind; b++)
					if (ind_sizes[b] > ind_sizes[a]) { size_t t = ind_sizes[a]; ind_sizes[a] = ind_sizes[b]; ind_sizes[b] = t; }
			ai += 2;
		} else {
			fprintf(stderr, "unknown or incomplete option: %s\n", argv[ai]);
			return 2;
		}
	}
	argv[ai - 1] = argv[0];
	argv += ai - 1;
	argc -= ai - 1;
	if (argc < 2) {
		fprintf(stderr, "usage: zscan [--max-txg N] [--ind-size N ...] [--max-vdev N] <device> [start] [end]\n");
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
	fprintf(stderr, "zscan %s: %" PRIu64 " .. %" PRIu64 " (device %" PRIu64 " bytes, max txg %" PRIu64 ")\n",
		argv[1], start, end, devsize, max_txg);

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
	fprintf(stderr, "done after %ld s\n", (long)(time(NULL) - t0));
	return 0;
}
