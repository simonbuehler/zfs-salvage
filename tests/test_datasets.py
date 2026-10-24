"""`zsread.py datasets` lists the same datasets and object set block pointers as `zdb -e -d`.

zdb (2.2.2, in the container of tests/zdb) is the reference; zsread walks MOS -> DSL itself and imports nothing.
Every fixture is checked undamaged and with its target's object set block destroyed: the target must then be
listed as unreadable instead of the walk crashing."""
import json
import os
import re
import subprocess
import unittest

import fx  # noqa: I001 - first: puts scripts/ on the path
import test_make_case as tmc

SCRIPT = r'''
set -u
cd /img/$V
for d in $DEVS; do :; done
VDEVS="$VDEVSPEC"
POOL=$(zdb -l /img/$V/d0.img | sed -n "s/^ *name: '\(.*\)'/\1/p" | head -1)
echo "POOL $POOL"
BPS=$(for d in $DEVS; do zdb -luuuu /img/$V/$d; done | awk '/txg = /{t=$3} /rootbp = /{sub(/.*rootbp = /,""); print t "\t" $0}' | sort -k1,1nr -u | cut -f2-)
mapfile -t BPA <<< "$BPS"
echo "## zsread"
ZSREAD_VDEVS="$VDEVS" python3 /s/zsread.py datasets "$POOL" "${BPA[@]:0:40}"
echo "## zdb"
mkdir -p /devs; for d in $DEVS; do ln -sf /img/$V/$d /devs/$d; done
zdb -e -p /devs -d "$POOL" 2>&1 | grep -E "^Dataset |failed to hold"
for ds in $(zdb -e -p /devs -d "$POOL" 2>&1 | sed -n 's/^Dataset \([^ ]*\) \[ZPL\].*/\1/p'); do
  echo "HDR $(zdb -e -p /devs -dddd "$ds" 0 2>&1 | grep -m1 '^Dataset ')"
done
'''


def run_in_container(images, variant, devices, vdevspec):
    r = subprocess.run(['docker', 'run', '--rm', '-e', f'V={variant}', '-e', f'DEVS={" ".join(devices)}',
                        '-e', f'VDEVSPEC={vdevspec}', '-v', f'{images}:/img', '-v', f'{fx.SCRIPTS}:/s:ro', tmc.IMAGE,
                        'bash', '-c', SCRIPT], capture_output=True, text=True, timeout=600)
    return r.stdout + r.stderr


def split(out):
    pool = re.search(r'^POOL (.*)$', out, re.M).group(1)
    zs = json.loads(next(l for l in out.split('## zsread\n')[1].splitlines() if l.startswith('{')))
    zdb = out.split('## zdb\n')[1]
    return pool, zs, zdb


@unittest.skipUnless(tmc.docker_ready(), 'needs docker (image with zdb 2.2.2, built from tests/zdb)')
class DatasetsLikeZdb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = fx.tmpdir()
        cls.img = os.path.join(cls.dir, 'img')

    @classmethod
    def tearDownClass(cls):
        fx.rmtree(cls.dir)

    def compare(self, variant, damaged=False, vdevspec=None):
        if not fx.have_images(variant):
            self.skipTest(f'no {variant} images')
        dest = os.path.join(self.img, variant)
        paths = fx.unpack(variant, dest)
        if damaged:
            fx.damage(dest, variant)
        devs = [os.path.basename(p) for p in paths]
        spec = vdevspec or f'0={",".join("/img/" + variant + "/" + d for d in devs)}'
        pool, zs, zdb = split(run_in_container(self.img, variant, devs, spec))
        self.assertIsNotNone(zs['rootbp_index'], f'no uberblock whose MOS verifies: {zs}')

        # names: zdb lists mos, snapshots and the readable datasets; failed ones separately
        zdb_ok = {m.group(1) for m in re.finditer(r'^Dataset (\S+) \[(?:ZPL|ZVOL)\]', zdb, re.M)}
        zdb_bad = set(re.findall(r"failed to hold dataset '([^']+)'", zdb))
        mine = {d['name']: d for d in zs['datasets']}
        self.assertEqual({n for n, d in mine.items() if d['readable']}, {n for n in zdb_ok if '@' not in n})
        self.assertEqual({n for n, d in mine.items() if not d['readable']}, {n for n in zdb_bad if '@' not in n})
        if damaged:
            self.assertTrue(any(not d['readable'] for d in mine.values()), 'the destroyed target must be listed as unreadable')

        # object set block pointer details of the readable ZPL datasets: zdb prints them with -dddd <ds> 0
        for line in re.findall(r'^HDR (Dataset .*)$', zdb, re.M):
            m = re.match(r'Dataset (\S+) \[(\w+)\], ID (\d+), cr_txg (\d+), \S+, (\d+) objects(?:, rootbp (.*))?$', line)
            name, typ, did, cr, objs, bp = m.groups()
            if name == 'mos':                       # `zdb -dddd <pool>` is the MOS, not the pool's root dataset
                continue
            d = mine[name]
            self.assertEqual((d['type'], d['id'], d['cr_txg'], d['objects']), (typ, int(did), int(cr), int(objs)), name)
            kind = re.search(r'\] (\S+) (\S+) (\S+)', bp or '')
            if kind:
                self.assertEqual((d['checksum'], d['compression'], d['encrypted']),
                                 (kind.group(1), kind.group(2), kind.group(3) != 'unencrypted'), name)
                self.assertEqual(d['objset_birth'], int(re.search(r'birth=(\d+)L', bp).group(1)), name)
        return pool, zs

    def test_every_undamaged_fixture(self):
        for variant, vdevs in (('legacy-single', None), ('modern-single', None), ('modern-mirror', None),
                               ('modern-sha256', None), ('modern-twodatasets', None), ('modern-zvol', None),
                               ('modern-deleted', None), ('modern-destroyed', None), ('unsupported-largednode', None),
                               ('unsupported-encrypted', None)):
            with self.subTest(variant):
                self.compare(variant, vdevspec=vdevs)

    def test_stripe_has_two_top_level_vdevs(self):
        if not fx.have_images('modern-stripe'):
            self.skipTest('no modern-stripe images')
        self.compare('modern-stripe', vdevspec='0=/img/modern-stripe/d0.img;1=/img/modern-stripe/d1.img')

    def test_damaged_target_is_listed_as_unreadable(self):
        for variant in ('legacy-single', 'modern-single', 'modern-twodatasets'):
            with self.subTest(variant):
                self.compare(variant, damaged=True)

    def test_zvol_type_and_large_dnode_flag(self):
        pool, zs = self.compare('modern-zvol')
        self.assertIn('ZVOL', {d.get('type') for d in zs['datasets']})
        if fx.have_images('unsupported-largednode'):
            dest = os.path.join(self.img, 'unsupported-largednode')
            names = [os.path.basename(p) for p in fx.unpack('unsupported-largednode', dest)]
            side = fx.sidecars()['unsupported-largednode']
            target = side['target']
            out = subprocess.run(['docker', 'run', '--rm', '-v', f'{self.img}:/img', '-v', f'{fx.SCRIPTS}:/s:ro', tmc.IMAGE, 'bash', '-c',
                                  f'P=$(zdb -l /img/unsupported-largednode/{names[0]} | sed -n "s/^ *name: .\\(.*\\).$/\\1/p" | head -1); '
                                  f'B=$(zdb -luuuu /img/unsupported-largednode/{names[0]} | awk \'/txg = /{{t=$3}} /rootbp = /{{sub(/.*rootbp = /,""); print t "\\t" $0}}\' | sort -k1,1nr | head -1 | cut -f2); '
                                  f'ZSREAD_VDEVS="0=/img/unsupported-largednode/{names[0]}" python3 /s/zsread.py datasets --large-dnodes {target} "$P" "$B"'],
                                 capture_output=True, text=True, timeout=300).stdout
            res = json.loads(next(l for l in out.splitlines() if l.startswith('{')))
            flags = {d['name']: d.get('large_dnodes') for d in res['datasets']}
            self.assertTrue(flags[target], flags)


if __name__ == '__main__':
    unittest.main()
