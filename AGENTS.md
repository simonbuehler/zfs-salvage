# AGENTS.md – working on zfs-salvage

Guide for contributors and AI coding assistants. Read README.md (what and why) and
docs/design.md (how) first.

## Invariants – never break these

1. **Read-only.** Nothing in this project may write to the pool's devices. Open devices with
   `O_RDONLY`, refuse to scan a device that is not read-only (`blockdev --getro`), never call
   `zpool import` without read-only devices. Transfer targets are refused when they are RAM or
   read-only mounts, and the target check itself must not create anything.
2. **Proof before trust.** A block counts only if it matches the checksum stored in its parent.
   Never "repair" data silently. A file whose blocks cannot all be verified is reported PARTIAL.
3. **No pool specifics in code.** Everything about one pool (devices, target dataset, root
   names, TXGs, layout) lives in the case file (`cases/<name>.json`) and is read via
   `scripts/common.py`. `zsread.py` gets its devices through `ZSREAD_DEVS`.
4. **No CDDL code.** Implement from the public on-disk format documentation, not by copying
   OpenZFS sources.
5. **No secrets** (keys, passwords) in the repository or in logs.

## Architecture

```
recovery host (has the disks)             workstation
  zscan  (C, static)    <---- ssh ----    scan-device.sh      chunks -> logs/raw/<dir>/chunk_NNNN.tsv.gz
  zsread.py batch       <---- ssh ----    rank-trees.py       -> status/probed.jsonl, target_candidates.json
  zsread.py tree        <---- ssh ----    web.py (catalog)    -> logs/tree/*.tsv -> catalog.py -> catalog.sqlite
  zsread.py extract     <---- ssh ----    transfer.py         -> tar stream -> target, manifest.tsv
  libzsalvage.so (C, ctypes)              status_page.py      -> status/index.html, workflow.json
```

- `zsread.py` runs on the recovery host, uses only the Python standard library, and loads
  `libzsalvage.so` if present (pure-Python fallback otherwise).
- The workstation side imports `common.py` for every path and setting.
- The web UI (`web.py`, `scripts/ui/`) is plain HTML/JS with no build step.

## Adding a feature (checklist)

1. Find the extension point: decompression and checksums are in `tools/libzsalvage/zsalvage.c`,
   in `zsread.py` (`read_bp` and its fallbacks) and in `tools/zscan/zscan.c` (`bp_ok` and the
   classification). The vdev mapping is in `zsread.py` `read_bp`. Dnode and attribute parsing are
   in `zsread.py` `Dnode` / `ZPL`.
2. Implement it with a pure-Python fallback where the reader has one.
3. Test against a **small pool image** that has the feature: create it with
   `truncate -s 256M img && zpool create -o ashift=9 -O compression=<x> test /path/img`, write
   known files, export, then run zscan + `zsread.py tree`/`extract` with
   `ZSREAD_DEVS=/path/img` and compare SHA-256 sums. To simulate the target situation, overwrite the
   dataset's objset block (find it with `zdb -dddd`) and check that the tree is still found.
4. Update the capability matrix in README.md and the `SUPPORTED` / `HINTS` tables in `make-case.py`.

## Conventions

- Code, comments and docs in English. Match the surrounding style. Prefer short functions and
  plain data (TSV/JSON lines) between stages.
- Every stage is resumable and logs what it did. Long jobs run detached and are restartable.
- Shell: `set -euo pipefail` where it fits. Never `pkill -f`/`pgrep -f` with a pattern that also
  matches your own command line.
- Keep the UI lean: one primary action per step.
