"""Shared helpers for the tests: find, unpack and damage fixtures, build the C tools locally.

Fixture images (tests/fixtures/<variant>/<dev>.xz) are not in git; tests that need them skip when they
are missing (regenerate with tests/vm/run.sh tests/make-fixtures.sh).
"""
import glob
import hashlib
import json
import lzma
import os
import shutil
import subprocess
import sys

TESTS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS)
FIXTURES = os.environ.get('ZFS_SALVAGE_FIXTURES') or os.path.join(TESTS, 'fixtures')
SCRIPTS = os.path.join(REPO, 'scripts')
# transfers to temporary directories must work where /tmp is a RAM disk; the target-safety tests switch this off
os.environ.setdefault('ZFS_SALVAGE_ALLOW_RAM_TARGET', '1')
ZSCAN = os.path.join(REPO, 'tools', 'zscan', 'zscan')
LIB = os.path.join(REPO, 'tools', 'libzsalvage', 'libzsalvage.so')

for p in (SCRIPTS, TESTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import make_fixtures  # noqa: E402  (apply_damage; importing it does not touch ZFS)


def _build(src, out, *flags):
    if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(src):
        return True
    r = subprocess.run(['gcc', '-O2', '-Wall', '-Wextra', *flags, '-o', out, src], capture_output=True, text=True)
    if r.returncode:
        sys.stderr.write(f'building {out} failed:\n{r.stderr}\n')
    return r.returncode == 0


def ensure_built():
    """Compile zscan and libzsalvage.so with the local gcc if missing or older than their sources."""
    ok = _build(os.path.join(REPO, 'tools', 'zscan', 'zscan.c'), ZSCAN)
    ok &= _build(os.path.join(REPO, 'tools', 'libzsalvage', 'zsalvage.c'), LIB, '-shared', '-fPIC')
    return bool(ok)


def _load(path):
    with open(path) as f:
        return json.load(f)


def sidecars():
    """All sidecars of real (non-derived) variants, by name."""
    out = {}
    for f in sorted(glob.glob(os.path.join(FIXTURES, '*.json'))):
        name = os.path.basename(f)[:-5]
        s = _load(f)
        if name != 'skipped' and 'base' not in s:
            out[name] = s
    return out


def derived():
    out = {}
    for f in sorted(glob.glob(os.path.join(FIXTURES, '*.json'))):
        s = _load(f)
        if 'base' in s:
            out[os.path.basename(f)[:-5]] = s
    return out


def have_images(name):
    s = sidecars().get(name)
    return bool(s) and all(os.path.exists(os.path.join(FIXTURES, name, d + '.xz')) for d in s['devices'])


def unpack(name, dest):
    """Unpack the images of a variant into dest (sparse), verify them against the sidecar.

    Returns the device file paths in the order of the sidecar's device list."""
    s = sidecars()[name]
    os.makedirs(dest, exist_ok=True)
    paths = []
    for dev in s['devices']:
        dst = os.path.join(dest, dev)
        h, total = hashlib.sha256(), 0
        with lzma.open(os.path.join(FIXTURES, name, dev + '.xz'), 'rb') as f, open(dst, 'wb') as out:
            for block in iter(lambda: f.read(1 << 20), b''):
                h.update(block)
                total += len(block)
                if block.count(0) == len(block):
                    out.seek(len(block), os.SEEK_CUR)               # keep holes sparse
                else:
                    out.write(block)
            out.truncate(total)
        if h.hexdigest() != s['image_sha256'][dev]:
            raise RuntimeError(f'{name}/{dev}: image does not match the sidecar (regenerate the fixtures)')
        paths.append(dst)
    return paths


def damage(dest, name):
    """Overwrite every copy of the target's objset block (the sidecar's damage spec)."""
    spec = (sidecars()[name].get('damage') or {}).get('spec')
    if not spec:
        raise RuntimeError(f'{name}: no damage spec')
    make_fixtures.apply_damage(dest, spec)


def tmpdir(prefix='zfs-salvage-test-'):
    import tempfile
    return tempfile.mkdtemp(prefix=prefix)


def rmtree(path):
    shutil.rmtree(path, ignore_errors=True)
