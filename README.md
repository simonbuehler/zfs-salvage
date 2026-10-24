# zfs-salvage

Recover files from a ZFS dataset that no longer opens, even when every copy of a critical
metadata block has been destroyed and `zpool import -F/-X/-T` and `zdb` all give up.

zfs-salvage scans the raw disk for the dataset's own block-pointer tree. It proves every block
it finds by its ZFS checksum, lists the files of each complete historical state, and copies
the files you choose to any machine. It never writes to the pool disks.

> **Read this first.** zfs-salvage only *reads* the pool's disks, but it comes with **no warranty**, and
> a recovery tool you are running is a recovery you are doing at your own risk. Never run `zpool import`
> on the damaged pool (even "read-only" imports write to writable devices). If a disk is clicking, drops
> off the bus or shows SMART errors, **make an image first** and work on the image
> ([Failing disks](#failing-disks)).
>
> Status: works on one real pool (see [Case study](#case-study)) and on a set of test pools. It is an
> early, experimental starter project with a narrow format coverage, not a polished product. See the
> [capability matrix](#capability-matrix).

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
   (lzjb, lz4 or uncompressed). It writes one TSV per 64 GiB chunk and can be resumed.
2. **Rank trees** (`scripts/rank-trees.py`): probes the candidates newest TXG first on the
   recovery host. A candidate counts only if the whole chain down to the root directory
   verifies by checksum. Verified trees are grouped by their top-level names; if there is more
   than one file system, you pick yours (or give its names in the case file).
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
| Vdev layout | single disk, mirror (any copy, both DVAs), several top-level vdevs (stripe of mirrors) | raidz, dRAID |
| Compression | off, lzjb, lz4 | zstd, gzip |
| Checksum | fletcher4, sha256 | blake3, skein, … |
| Blocks | regular and embedded block pointers, 16 KiB and 128 KiB indirect blocks | gang blocks |
| Dnodes | 512-byte dnodes, SA and legacy znode attributes | large dnodes, spill blocks |
| Datasets | file systems; zvols (copied as one sparse `.raw` image) | clones' origin chains, snapshots as separate trees |
| Encryption | below ZFS (LUKS etc., opened read-only) | native ZFS encryption |
| Targets | local path (WSL drive letters too), SSH to Linux/Windows, recovery host disk | — |

The pool's parameters live in a **case file** (`cases/<name>.json`), not in the code.
`scripts/make-case.py` creates the case file from `zdb` output. It asks only for what it cannot detect, such as the
target dataset and its expected top-level names, and warns about unsupported features.

## Quick start

You need one computer that has the pool's disks attached, booted from an
[Ubuntu 24.04 desktop live USB](https://ubuntu.com/download/desktop) ("Try Ubuntu"), a second disk with free space for the
rescued files, and a network connection for the download. Everything runs on that one computer;
the web UI opens in its browser.

1. **Boot the live system** and connect to the network. Do not install Ubuntu, do not import the pool.
2. **Open the pool's devices read-only.** Plain partitions: `sudo blockdev --setro /dev/sdX3`.
   LUKS: `sudo cryptsetup open --readonly /dev/sdX4 ro_sdX4` (the tool never asks for passwords).
   Mirrors and stripes: do this for every disk of the pool. Check with `sudo zdb -l /dev/sdX3`
   (it should print a label with the pool's name).
3. **Attach and mount the disk that will hold the rescued files** (a click in the file manager is
   enough). It must be a real disk, not the live system itself: that lives in RAM and is gone after a reboot.
4. **Download the release bundle onto that disk** and unpack it there:
   `tar xzf zfs-salvage-*.tar.gz && cd zfs-salvage-*`
5. **Check the machine:** `sudo ./zfs-salvage doctor`. Fix every `FAIL` ([Troubleshooting](#troubleshooting)).
   `zdb` comes with the live image; if it is missing: `sudo apt install zfsutils-linux`.
6. **Describe the pool:** `sudo ./zfs-salvage make-case --dev /dev/sdX3` (one `--dev` per disk).
   It reads the labels, tells you what the pool uses and whether that is supported, and writes the case file.
   You can leave "names you expect in the root directory" empty.
7. **Scan the disks.** Open two more terminal windows in the same folder. In the first run
   `sudo ./zfs-salvage scan /dev/sdX3`, in the second `sudo ./zfs-salvage rank --loop`. For a stripe
   scan one disk of every top-level vdev, for a mirror any one disk. A scan reads at about 150–190 MB/s (a 1 TB
   disk takes about 1½ hours); stop it with Ctrl+C and start the same command again to resume.
8. **Open the web UI:** `sudo ./zfs-salvage` (it runs the checks again, then opens http://localhost:8765).

Then follow the three steps in the UI:

1. **Find a state:** scan progress, the file systems found (pick yours if there are several), the best
   recoverable state with its date and distance to the last change, and the button that builds or updates the catalog.
2. **Select files:** folder tree with tri-state checkboxes, quick filters (photos & videos,
   documents, everything without hidden folders), name, date and size filters, and a list of unreadable entries.
3. **Transfer:** pick this computer, another computer (hosts from `~/.ssh/config`) or a disk of the
   machine. Drive suggestions show free space. Progress, resume, the manifest and **verify copy** (hashes every
   file on the target again) are available per transfer.

All results (scan chunks, catalog, case file, logs) are in `data/` next to `zfs-salvage`. Delete that
folder to start over; the pool's disks are never touched.

### Failing disks

A disk that already shows read errors can die while you scan it, and every pass over it costs
life. Copy it once with `ddrescue`, put the image on the target disk, make it read-only, and give the
image to the tool instead of the device:

```bash
sudo apt install gddrescue
sudo ddrescue -d -r0 /dev/sdX3 /mnt/target/disk0.img /mnt/target/disk0.map   # first pass: everything that reads easily
sudo ddrescue -d -r3 /dev/sdX3 /mnt/target/disk0.img /mnt/target/disk0.map   # second pass: retry the bad areas
chmod a-w /mnt/target/disk0.img
sudo ./zfs-salvage make-case --dev /mnt/target/disk0.img
```

The image holds the complete partition, so it needs as much free space as the partition is large.

### Advanced: the disks are in another machine (remote mode)

The tool can also run on your workstation while the disks sit in a recovery machine reachable over
SSH (alias `recovery-host`, or `ZFS_SALVAGE_HOST=<host>`; passwordless key login is required). On the recovery machine
open the devices read-only and run `make-case.py` there, copy the case file into `data/cases/` on the workstation, then
`scripts/deploy-tools.sh` installs `zscan`, `libzsalvage.so` and `zsread.py` on the recovery machine (repeat after every
reboot of a live system), and scan, ranking and the web UI run on the workstation as above.
`./zfs-salvage doctor` checks the connection.

### Troubleshooting

`./zfs-salvage doctor` prints one line per check: `OK`, `WARN` (works, but look at it) or `FAIL` (fix first).

| Check | Message | What to do |
|---|---|---|
| root | not running as root | Start with `sudo ./zfs-salvage` (reading the devices needs root). `--no-root` only looks around. |
| python | python x is too old | Use a current live system; Ubuntu 24.04 has 3.12. |
| zdb | zdb is not installed | `sudo apt install zfsutils-linux`. Only `zdb -l` is used; the pool is never imported. |
| zdb | zdb found, but no ZFS member device to test it on | Attach the pool's disks (opened read-only) and run `doctor` again. |
| zdb | `zdb -l …` shows no ZFS label | The device is not a pool member: use the partition (`/dev/sdX3`) or the opened LUKS mapper, not the whole disk, and run as root. |
| zscan | zscan is not built; zscan is too old; zscan does not run here | Use the release bundle. From a git checkout: `tools/build-portable.sh` (needs Docker). |
| zscan | zscan is not installed on <host>; zscan on <host> is too old; cannot check zscan | Remote mode: `scripts/deploy-tools.sh` (repeat after every reboot of a live system); the last one means the host is not reachable. |
| libzsalvage | libzsalvage.so cannot be used | Reading still works but is much slower. Use the release bundle, or `tools/build-portable.sh`. |
| data directory | the data directory is in RAM | You are running from the live system's RAM disk. Unpack the bundle on a real disk and start it from there. |
| data directory | only N MB free | Free space or use another disk (1 GB is the minimum; the catalog of a large pool takes some hundred MB). |
| devices | no ZFS member devices found | Attach the pool's disks; open encrypted ones with `cryptsetup open --readonly …`. |
| devices | device(s) of the case do not exist here | The disks are attached to another machine (remote mode: set `ZFS_SALVAGE_HOST`), or were not opened yet. |
| devices | not read-only | `sudo blockdev --setro <device>` (image files: `chmod a-w`). This sets a kernel flag; it does not write to the disk. |
| case file | no case file yet | Normal at the start: run `./zfs-salvage make-case --dev …`. |
| case file | the case file is not valid | Create it again with `./zfs-salvage make-case`. |
| pool features | this pool has properties that are not supported | See the [capability matrix](#capability-matrix). Tools that carve files by content (e.g. PhotoRec) can still find files. |
| memory | only N GB RAM | Close other programs; very large pools need more. |
| recovery host | cannot reach … over ssh | Remote mode only: set up a key login (`ssh-copy-id`), or set `ZFS_SALVAGE_HOST=local` when the disks are in this machine. |

## Repository layout

| Path | Purpose |
|---|---|
| `zfs-salvage` | launcher: `doctor`, `make-case`, `scan`, `rank`, `web`, or just `sudo ./zfs-salvage` |
| `scripts/doctor.py` | the machine check behind `./zfs-salvage doctor` |
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
| `tools/build-portable.sh` | builds `zscan` (static) and `libzsalvage.so` for glibc 2.17 and newer (Docker) |
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
for humans and AI coding assistants alike). The reader is small and table-driven. Typical next steps are raidz column mapping, zstd and gang blocks. Each feature needs one test against a known
pool image. Add the feature to the capability matrix and to the compatibility check in `make-case.py`.

## License

Not decided yet.
