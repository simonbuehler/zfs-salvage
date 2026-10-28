#!/usr/bin/env python3
"""Build the demo pool "tank" from dev/demo/build/content (run as root with ZFS, normally in the test VM:
tests/vm/run.sh dev/demo/make-pool.sh).

The two batches of files are written in separate transactions, so the pool has older and newer states; then every copy of
the object set block of tank/home is destroyed – the damage zfs-salvage works around. Output in dev/demo/build/:
tank.img.xz (the damaged pool), tank.json (pool facts and the damage spec).
"""
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), 'tests'))
import make_fixtures as mf  # noqa: E402

BUILD = os.path.join(HERE, 'build')
WORK = '/var/tmp/zfs-salvage-demo'
SIZE = 256 << 20


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    dev = os.path.join(WORK, 'tank.img')
    with open(dev, 'wb') as f:
        f.truncate(SIZE)
    mnt = '/mnt/tank'
    # the clock of the VM goes back to 2014, so the pool's states carry the dates of the photos
    subprocess.run(['timedatectl', 'set-ntp', 'false'], capture_output=True)
    mf.sh('date', '-s', '2014-09-20 18:30:00')
    mf.sh('zpool', 'create', '-f', '-o', 'ashift=12', '-O', f'mountpoint={mnt}', '-O', 'atime=off', 'tank', dev)
    mf.sh('zfs', 'create', 'tank/home')
    for batch, when in (('batch1', '2014-09-20 18:40:00'), ('batch2', '2014-10-12 21:15:00')):
        mf.sh('date', '-s', when)
        mf.sh('cp', '-a', os.path.join(BUILD, 'content', batch) + '/.', f'{mnt}/home/')
        mf.sh('zpool', 'sync', 'tank')
    mf.sh('zpool', 'export', 'tank')
    ds, _ = mf.datasets(WORK, 'tank')
    _, _, first = mf.meta_dnode(WORK, 'tank', 'home')
    bp, _ = mf.parse_dataset_lines(first)
    ds['tank/home'].update(bp['tank/home'])
    spec, _ = mf.damage_spec('tank/home', [dev], 'plain', ds['tank/home']['dvas'])
    mf.apply_damage(WORK, spec)
    r = subprocess.run(['zdb', '-e', '-p', WORK, '-d', 'tank/home'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    side = {'pool': 'tank', 'target': 'tank/home', 'label_start': mf.LABEL_START, 'newest_txg': mf.newest_txg(dev),
            'damage': spec, 'zdb_after_damage': r.stdout.strip()[-400:], 'zfs_version': mf.sh('zfs', 'version').split()[0]}
    mf.sh('xz', '-T0', '-9', '-k', '-f', dev)
    shutil.move(dev + '.xz', os.path.join(BUILD, 'tank.img.xz'))
    with open(os.path.join(BUILD, 'tank.json'), 'w') as f:
        json.dump(side, f, indent=1)
    shutil.rmtree(WORK)
    print('tank:', side['zdb_after_damage'].splitlines()[-1] if side['zdb_after_damage'] else 'no zdb output')


if __name__ == '__main__':
    main()
