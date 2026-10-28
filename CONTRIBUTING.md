# Contributing to zfs-salvage

Thanks for helping. Read [AGENTS.md](AGENTS.md) first: it has the invariants (read-only, proof before
trust, no pool specifics in code, no CDDL code) and the architecture. This file is about the development
setup.

## Setup

Linux or WSL2 with Python 3.8+ and gcc. Nothing else is needed for the first two levels below. In VS Code or
GitHub Codespaces, "Reopen in Container" gives you that environment with `zdb` included (`.devcontainer/`).

```bash
git clone https://github.com/simonbuehler/zfs-salvage
cd zfs-salvage
make test
```

## What you need for which work

| Level | Command | Needs | When |
|---|---|---|---|
| 1 | `make test` | Python 3, gcc | always; about 40 s, tests that need fixture pools skip |
| 2 | `make fixtures`, then `make test-full` | curl (30 MB download) | any change to the reader, scanner, ranking or transfer |
| 3 | `make fixtures-regen` | root and the ZFS kernel module, or Docker and `/dev/kvm` for the test VM | a new fixture variant (a pool feature or layout) |
| 4 | `make portable`, `make bundle` | Docker | releases; the CI does this for every tag |

`make help` lists all targets.

Notes for WSL2: its kernel has no ZFS module, so level 3 uses the test VM (`tests/vm/run.sh`: QEMU/KVM inside
Docker). `/dev/kvm` exists in WSL2 on Windows 11 with nested virtualisation; add yourself to the `kvm` group.
Levels 1, 2 and 4 work as on any Linux.

## Fixture pools

The tests run the whole pipeline on small pools with known content (`tests/make_fixtures.py`): every variant is a
real pool created by ZFS, with a sidecar JSON in git (`tests/fixtures/<variant>.json`: what was written, where the
blocks are, how it is damaged). The images themselves are not in git; `make fixtures` downloads the release asset
named in `tests/fixtures/RELEASE` and checks it against `tests/fixtures/SHA256SUMS`.

To add a variant: describe it in `tests/make_fixtures.py`, run `make fixtures-regen` (all variants are built again,
the images are not bit-reproducible), map it to a capability in `tests/capability_map.json`, and add an entry to
`XFAIL` in `tests/run.py` if it is expected to fail for now. A maintainer packs the images (`make fixtures-pack`),
uploads them as a new `fixtures-N` release and updates `tests/fixtures/RELEASE` in the same pull request.

## Pull requests

- One topic per pull request. `make test` must pass; for reader, scanner or transfer changes also `make test-full`.
- A new capability comes with a fixture variant and an updated matrix (`make matrix`; CI fails when the README
  matrix and the test results disagree).
- Code, comments and docs in English. Match the surrounding style; standard library only on the Python side.
- Never commit pool images, case files of real pools, logs or anything personal.

## Releases

Tag `vX.Y.Z` on `main`. The release workflow builds the portable binaries, runs all tests on the fixture pools,
builds `zfs-salvage-vX.Y.Z.tar.gz` with `SHA256SUMS` and publishes them as a GitHub release.

## Repository layout

| Path | Purpose |
|---|---|
| `zfs-salvage` | launcher: `doctor`, `make-case`, `scan`, `rank`, `web`, or just `sudo ./zfs-salvage` |
| `scripts/doctor.py` | the machine check behind `./zfs-salvage doctor` |
| `scripts/make-case.py` | case file generator (reads labels and uberblocks with `zdb -l`; never imports) |
| `scripts/scan-device.sh` | chunked, resumable scan driver |
| `tools/zscan/` | raw-device scanner (C, static in releases) |
| `scripts/rank-trees.py` | candidate verification and ranking |
| `scripts/zsread.py` | userspace ZFS reader: `probe`, `tree`, `batch`, `extract`, `datasets` |
| `tools/libzsalvage/` | fletcher4, lzjb, lz4, verified block reads (C, used via ctypes) |
| `scripts/catalog.py` | tree listing → SQLite catalog |
| `scripts/web.py`, `scripts/ui/` | web UI (setup, status, selection, transfer) |
| `scripts/setup_api.py`, `scripts/jobs.py` | the setup page and its restartable jobs |
| `scripts/status_page.py` | status page and chain diagram |
| `scripts/transfer.py` | transfer jobs, target checks, manifests |
| `scripts/common.py` | shared settings: case file, paths |
| `scripts/diag/` | expert diagnostics: zdb survey per TXG, objset block inspector |
| `cases/example.json` | the case file format |
| `tools/build-portable.sh` | builds `zscan` and `libzsalvage.so` for glibc 2.17 and newer (Docker) |
| `tests/` | unit and integration tests, fixture generator, pipeline runner (`run.sh`), test VM (`vm/`) |
| `dev/` | maintainer tools: release bundle, capability matrix generator, demo video |
| `docs/design.md` | how scanning, ranking and reading work |
