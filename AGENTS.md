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

Everything runs on the machine that has the pool's disks (usually a live system). The stages are
separate processes that pass plain files:

```
scan-device.sh  -> zscan (C, static)       chunks -> <data>/logs/raw/<dir>/chunk_NNNN.tsv.gz
rank-trees.py   -> zsread.py batch         -> <data>/status/probed.jsonl, target_candidates.json
web.py          -> zsread.py tree          -> <data>/logs/tree/*.tsv -> catalog.py -> catalog.sqlite
transfer.py     -> zsread.py extract       -> tar stream -> target folder (local or ssh), manifest.tsv
status_page.py                             -> <data>/status/index.html, workflow.json
```

- `zsread.py` uses only the Python standard library and loads `libzsalvage.so` (C, via ctypes) if
  present, with a pure-Python fallback otherwise.
- Every other script imports `common.py` for every path and setting.
- The only network use is an optional ssh transfer target. To use the web UI from another
  computer, forward its port (`ssh -L 8765:localhost:8765 <machine>`).
- The web UI (`web.py`, `scripts/ui/`) is plain HTML/JS with no build step.

## Adding a feature (checklist)

1. Find the extension point: decompression and checksums are in `tools/libzsalvage/zsalvage.c`,
   in `zsread.py` (`read_bp` and its fallbacks) and in `tools/zscan/zscan.c` (`bp_ok` and the
   classification). The vdev mapping is in `zsread.py` `read_bp`. Dnode and attribute parsing are
   in `zsread.py` `Dnode` / `ZPL`.
2. Implement it with a pure-Python fallback where the reader has one.
3. Add a fixture variant that has the feature to `tests/make_fixtures.py` and build the pools
   (`make fixtures-regen`; on WSL or without the ZFS module this runs in the test VM). Every variant
   also gets a damaged form with the dataset's objset block destroyed, so the tests check that the tree
   is still found. Quick experiments without the fixture machinery: run zscan and
   `zsread.py tree`/`extract` on an image with `ZSREAD_DEVS=/path/img` and compare SHA-256 sums.
4. Map the variant to a capability in `tests/capability_map.json`, regenerate the README matrix
   (`make matrix`) and update `SUPPORTED` / `REFUSALS` in `make-case.py`.
5. `make test-full` must pass. Setup and the levels of testing: [CONTRIBUTING.md](CONTRIBUTING.md).

## Conventions

- Code, comments and docs in English. Match the surrounding style. Prefer short functions and
  plain data (TSV/JSON lines) between stages.
- Every stage is resumable and logs what it did. Long jobs run detached and are restartable.
- Shell: `set -euo pipefail` where it fits. Never `pkill -f`/`pgrep -f` with a pattern that also
  matches your own command line.
- Keep the UI lean: one primary action per step.
