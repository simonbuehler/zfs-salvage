# Design

Goal: recover the files of a ZFS dataset whose path from the pool root is broken, read-only,
with every recovered block proven by its ZFS checksum.

## Starting point

ZFS reaches a file through a chain: uberblock → MOS → DSL dataset → **objset block** →
meta-dnode tree (indirect blocks L6…L1, dnode blocks L0) → dnode → file blocks. Each link
stores the checksum of the next one. If one link is destroyed in all copies, everything below
it is unreachable, although it may be fully intact.

zfs-salvage re-enters the chain below the broken link: it finds the root of the meta-dnode tree
(the L6 indirect block, or an older objset block) by scanning the disk, and verifies downwards.

## Phases

1. **Scan (`tools/zscan`, C, on the recovery host).** Reads the device sequentially. At every
   512-byte position it tries lzjb and lz4 decompression with early abort (and the uncompressed
   interpretation if the start looks like a dnode or block pointer) and classifies:
   - `OBJSET`: dnode 0 of type DNODE with plausible fields, `os_type == ZPL`
   - `IND`: indirect block of a dnode tree: 128 block pointers of type DNODE on the same level,
     known compression and checksum, plausible birth
   - `DNODES`: dnode block: slots empty or plausible `dnode_phys_t`

   Each find is written with offset, kind, physical/logical size, fletcher4 over psize and
   details (level, maximum child birth, number of entries).
2. **Rank (`scripts/rank-trees.py` + `zsread.py batch`).** Candidates are the tree roots
   (L6 blocks, plus objset blocks whose L6 was not found). Identical content is probed once.
   A candidate counts only if its children satisfy the checksums stored in it, down to the ZPL
   master node and root directory. The target dataset is recognised by its top-level names
   (`target.root_names` in the case file). Newest TXG first.
3. **Catalog (`zsread.py tree` → `catalog.py`).** Full listing of one state: path, type,
   size, mtime, object number, readability.
4. **Extraction (`zsread.py extract`).** Files are read block by block. Every block is
   checked against the checksum in its block pointer, falling back to the other DVA and the other
   device. Output is a tar stream with original mtimes, plus a manifest per file (OK / PARTIAL
   / error, SHA-256, bad blocks).

## Why it works well

- The pool's geometry, compression, checksum and TXG timeline are known from the case file,
  so recognition is narrow and fast (~180 MB/s on a 3 TB disk).
- Many historical states: there is a fallback for every subtree, not just one global state.
- All copies (mirror halves and ditto blocks) serve as read sources.
- Everything is open, reproducible and logged. Nothing is ever written to the pool devices.

## Limits

See the capability matrix in [README.md](../README.md). The main extension points are
decompressors and checksums (`tools/libzsalvage`, `zscan`), the vdev mapping for raidz
and several top-level vdevs (`zsread.py` `read_bp`), and gang blocks.
