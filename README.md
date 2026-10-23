# zfs-salvage

Recover files from a ZFS dataset that no longer opens, even when every copy of a critical
metadata block has been destroyed and `zpool import -F/-X/-T` and `zdb` all give up.

zfs-salvage scans the raw disk for the dataset's own block-pointer tree. It proves every block
it finds by its ZFS checksum, lists the files of each complete historical state, and copies
the files you choose to any machine. It never writes to the pool disks.

> Status: works on one real pool (see [Case study](#case-study)). It is a starter project
> with a narrow format coverage, not a polished product. See the [capability matrix](#capability-matrix).

## When it helps

- The pool imports (or `zdb` can open the MOS), but a dataset fails with
  `can't open objset for <dataset>` / `ECKSUM` / `cannot iterate filesystems`.
- Rewinding to older TXGs does not help, because all of them still point to the same broken block.
- File contents and most dnodes are probably still on disk. Only the path to them is gone.

It does **not** help when the data blocks themselves are overwritten or the disks are unreadable.

## How it works

```
ZFS normally:  uberblock → MOS → dataset → objset block → dnode tree (L6…L0) → dnodes → file data
                                               ✗ destroyed
zfs-salvage:   scan disk → find dnode-tree roots (L6 / objset blocks) → verify children by checksum
               → reconstruct directory tree (ZPL) → pick a state → extract with per-block verification
```

1. **Scan** (`tools/zscan`, C): reads the device sequentially (~160–190 MB/s) and classifies
   every 512-byte position as objset block, indirect block of a dnode tree, or dnode block
   (lzjb or uncompressed). It writes one TSV per 64 GiB chunk and can be resumed.
2. **Rank trees** (`scripts/rank-trees.py`): probes the candidates newest TXG first on the
   recovery host. A candidate counts only if the whole chain down to the root directory
   verifies by checksum. It identifies the target dataset by its top-level names.
3. **Catalog** (`scripts/catalog.py`): lists the full tree of a chosen state (path, size,
   mtime, readability) into SQLite.
4. **Select & transfer** (web UI): choose files by folder, type, name or date, then copy them
   locally, over SSH (Linux or Windows) or to a disk on the recovery host. Every block is checked
   against its checksum, with fallback to the other DVA and the other disk. Original mtimes are
   kept. A manifest records result and SHA-256 per file. Interrupted transfers resume.

## Safety rules

1. **Never write to the pool disks.** Open encryption layers read-only
   (`cryptsetup open --readonly …`) and work only on those mappings or on images.
   "Read-only" imports with writable devices *do* write uberblocks.
2. No secrets in the repository. Keyfiles stay in RAM (`/run/…`).
3. Check that a transfer target is a real, writable disk. The UI refuses RAM disks and
   read-only mounts.

## Capability matrix

| Area | Supported | Not yet |
|---|---|---|
| Vdev layout | single disk, mirror (any copy, both DVAs) | raidz, dRAID |
| Compression | off, lzjb | lz4, zstd, gzip |
| Checksum | fletcher4 | sha256, blake3, … |
| Blocks | regular block pointers, 16 KiB indirect/dnode blocks | gang blocks, embedded block pointers |
| Dnodes | 512-byte dnodes, SA and legacy znode attributes | large dnodes, spill blocks |
| Encryption | below ZFS (LUKS etc., opened read-only) | native ZFS encryption |
| Targets | local path (WSL drive letters too), SSH to Linux/Windows, recovery host disk | — |

The pool's parameters live in a **case file** (`cases/<name>.json`), not in the code.
`scripts/make-case.py` creates the case file from `zdb` output. It asks only for what it cannot detect, such as the
target dataset and its expected top-level names, and warns about unsupported features.

## Quick start

You need two machines: the **recovery host** with the pool's disks attached (e.g. SystemRescue
with ZFS, booted from USB; it needs python3 and, for `make-case.py`, zdb), and your
**workstation** with SSH access to it (alias `recovery-host`, or set `ZFS_SALVAGE_HOST=<host>`).
They can be the same machine.

On the **recovery host**, open the devices read-only and describe the pool:

```bash
cryptsetup open --readonly --type luks1 --key-file /run/key /dev/sdX4 ro_sdX4   # if encrypted
python3 scripts/make-case.py --dev /dev/mapper/ro_sdX4 ...   # writes cases/<name>.json
```

Copy the case file to `cases/` on the workstation, then on the **workstation**:

```bash
scripts/deploy-tools.sh                                   # zscan, libzsalvage.so, zsread.py -> recovery host
scripts/scan-device.sh /dev/mapper/ro_sdX4 >> logs/scan_ro_sdX4.log &   # resumable, chunk by chunk
python3 -I scripts/rank-trees.py --loop >> logs/rank-trees.log &       # verify candidates as chunks finish
python3 -I scripts/web.py                                 # http://localhost:8765
```

Release bundles contain prebuilt `zscan` and `libzsalvage.so`. From a git checkout, build them
with `tools/zscan/build.sh` and `tools/libzsalvage/build.sh` (Docker).

The web UI walks you through three steps:

1. **Find a state:** scan progress, the best recoverable state with its date and distance to
   the last change, and the button that builds or updates the catalog.
2. **Select files:** folder tree with tri-state checkboxes, quick filters (photos & videos,
   documents, everything without hidden folders), name, date and size filters, and a list of unreadable entries.
3. **Transfer:** pick this computer, another computer (hosts from `~/.ssh/config`) or the
   recovery host. Drive suggestions show free space. Progress, resume and the manifest are available per target.

## Repository layout

| Path | Purpose |
|---|---|
| `tools/zscan/` | raw-device scanner (static C binary) |
| `tools/libzsalvage/` | fletcher4, lzjb, verified block reads (C, used via ctypes) |
| `scripts/zsread.py` | userspace ZFS reader: `probe`, `tree`, `batch`, `extract` (runs on the recovery host) |
| `scripts/deploy-tools.sh` | installs zscan, libzsalvage.so and zsread.py on the recovery host |
| `scripts/scan-device.sh` | chunked, resumable scan driver |
| `scripts/rank-trees.py` | candidate verification and ranking |
| `scripts/catalog.py` | tree listing → SQLite catalog |
| `scripts/web.py`, `scripts/ui/` | web UI (status, selection, transfer); regenerates the status page |
| `scripts/status_page.py` | status page and chain diagram |
| `scripts/transfer.py` | transfer jobs, target checks, manifests |
| `scripts/common.py` | shared settings: case file, recovery host, paths |
| `scripts/make-case.py` | case file generator |
| `scripts/make-bundle.sh` | release tarball with prebuilt binaries |
| `scripts/diag/` | zdb survey per TXG; objset block inspector |
| `cases/` | case files (pool profile, timeline, target); `example.json` shows the format |
| `docs/` | design notes, the case analysis, issue archive |

## Case study

A Gentoo NAS crashed in October 2014 (ZFS 0.6.3, mirror on LUKS). Afterwards `rpool/home`
(360 GB, 52,333 objects) failed with `can't open objset … ECKSUM`. That was
[openzfs/zfs#2831](https://github.com/openzfs/zfs/issues/2831), later
[#17809](https://github.com/openzfs/zfs/issues/17809).

- With current OpenZFS master (deadlist fix PR #19515) the pool opens at 54 TXGs. All MOS
  metadata of the dataset is intact, only its **objset block** is broken. Both copies of that
  block (two DVAs on both mirror halves) contain unrelated data, so the space was most likely
  allocated twice around the crash.
- The scan found **929 complete, checksum-verified states** of the dataset. The newest
  one is from 2014-10-23 03:23 (TXG 728,869, 52,333 objects), 16.6 hours before the dataset's last change.
- Files are copied with every block verified. The first restores were byte-exact.

Details: docs/analysis-2026-10-23.md,
[docs/design.md](docs/design.md).

## Extending it

See [AGENTS.md](AGENTS.md) for the architecture, invariants and how to add a feature (written
for humans and AI coding assistants alike). The reader is small and table-driven. Typical next steps are lz4 decompression, sha256
checksums, raidz column mapping and gang blocks. Each feature needs one test against a known
pool image. Add the feature to the capability matrix and to the compatibility check in `make-case.py`.

## License

Not decided yet.
