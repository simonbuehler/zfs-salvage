"""Building the catalog from any verified state, not only the newest: files that were deleted since, and a
dataset that was destroyed, are still reachable through their older states."""
import hashlib
import json
import os
import stat
import subprocess
import sys
import unittest

import fx  # noqa: I001 - first: puts scripts/ on the path
from test_e2e_local import api, free_port, wait
from test_tree_groups import read_json
from test_visible_errors import env_for

def read_text(path):
    with open(path) as f:
        return f.read()


EVERYTHING = {'q': '', 'cat': '', 'from': '', 'to': '', 'min': '', 'under': '/', 'nohidden': ''}


class Rescue:
    """A fixture pool on one machine: scan, rank, web server; the case knows no root names."""

    def __init__(self, variant):
        self.variant = variant
        self.side = fx.sidecars()[variant]
        self.work = fx.tmpdir()
        self.proc = None
        dev = fx.unpack(variant, os.path.join(self.work, 'img'))[0]
        os.chmod(dev, os.stat(dev).st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        raw = os.path.join(self.work, 'raw')
        case = {'name': variant, 'pool': {'name': self.side['pool'], 'devices': [{'path': dev, 'vdev': 0}]},
                'profile': {'label_start': self.side['label_start'], 'compression': [], 'checksum': []},
                'timeline': [{'txg': self.side['newest_txg'], 'time': 1}], 'compatibility': [],
                'target': {'dataset': self.side['target'], 'root_names': []},
                'scan': {'device': 'd0', 'chunk_dir': raw, 'log': os.path.join(self.work, 'scan.log')}}
        self.case_path = os.path.join(self.work, 'case.json')
        with open(self.case_path, 'w') as f:
            json.dump(case, f)
        self.state = os.path.join(self.work, 'state')
        self.env = env_for(self.state, 'down', ZFS_SALVAGE_CASE=self.case_path,
                           ZFS_SALVAGE_CATALOG_DIR=os.path.join(self.work, 'catalog'))
        del self.env['ZFS_SALVAGE_HOST']
        self.env.pop('ZFS_SALVAGE_READER', None)
        r = subprocess.run(['bash', os.path.join(fx.SCRIPTS, 'scan-device.sh'), dev, '64', raw], env=self.env,
                           capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stdout[-500:]
        self.rank()
        self.port = free_port()
        self.proc = subprocess.Popen([sys.executable, '-I', os.path.join(fx.SCRIPTS, 'web.py')],
                                     env=dict(self.env, SALVAGE_UI_PORT=str(self.port)),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait(lambda: api(self.port, '/api/workflow'), 'the web server')

    def rank(self):
        r = subprocess.run([sys.executable, '-I', os.path.join(fx.SCRIPTS, 'rank-trees.py')], env=self.env,
                           capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr[-500:]

    @property
    def candidates(self):
        return read_json(os.path.join(self.state, 'status', 'target_candidates.json'))

    def choose(self, marker):
        """Pick the group that has `marker` among its names, as a click on "This one" does."""
        groups = api(self.port, '/api/target/groups')['groups']
        group = next(g for g in groups if marker in g['names'])
        api(self.port, '/api/target/choose', {'names': group['names']})
        return wait(lambda: self.candidates or None, 'the candidates of the chosen group', 60)

    def build_catalog(self, row):
        cand = dict(row['candidate'], txg=row['birth'])
        api(self.port, '/api/catalog/rebuild', {'candidate': cand})
        job = wait(lambda: (lambda j: j if j['state'] in ('done', 'failed') else None)(api(self.port, '/api/catalog/status')),
                   'the catalog build')
        assert job['state'] == 'done', job
        return api(self.port, '/api/summary')

    def rescue(self, files):
        """Select everything, transfer, and compare the files with the sidecar's SHA-256."""
        api(self.port, '/api/select', {'op': 'reset'})
        api(self.port, '/api/select', {'op': 'add', 'filter': EVERYTHING})
        dest = os.path.join(self.work, 'out')
        api(self.port, '/api/transfer/start', {'type': 'local', 'dest': dest})
        wait(lambda: next((s for s in api(self.port, '/api/transfer/status') if s['state'] == 'done'), None), 'the transfer')
        for f in files:
            with open(os.path.join(dest, f['path'].lstrip('/')), 'rb') as fh:
                assert hashlib.sha256(fh.read()).hexdigest() == f['sha256'], f['path']

    def close(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        fx.rmtree(self.work)


@unittest.skipUnless(fx.have_images('modern-deleted') and fx.ensure_built(), 'needs gcc and the modern-deleted images')
class DeletedFiles(unittest.TestCase):
    def test_state_before_the_deletion(self):
        r = Rescue('modern-deleted')
        try:
            gone = r.side['deleted_prefix']
            files = [f for f in r.side['files'] if f['type'] == 'f']
            deleted = [f for f in files if f['path'].startswith(gone)]
            self.assertTrue(deleted)
            rows = r.choose('photos')
            self.assertGreaterEqual(len(rows), 2, 'newest state and the ones before the deletion')
            newest, before = rows[0], next(x for x in rows if x['birth'] <= r.side['txg_before_delete'])
            self.assertGreater(before['objects'], newest['objects'] - 1)

            # the newest state does not have the folder
            summary = r.build_catalog(newest)
            self.assertEqual(summary['files'], len(files) - len(deleted))
            # the state before the rm has it all
            summary = r.build_catalog(before)
            self.assertEqual(summary['files'], len(files))
            r.rescue(files)                               # including the deleted ones, byte-exact

            # the status page offers every state, marks the one in the catalog, and says what a longer wait means
            page = os.path.join(r.state, 'status', 'index.html')
            wait(lambda: os.path.exists(page) and 'recoverable states' in read_text(page), 'the status page', 60)
            html = read_text(page)
            self.assertEqual(html.count('rebuildFrom(this)'), len(r.candidates) - 1)
            self.assertIn('in the catalog', html)
            self.assertIn('overwritten', html)
        finally:
            r.close()


@unittest.skipUnless(fx.have_images('modern-destroyed') and fx.ensure_built(), 'needs gcc and the modern-destroyed images')
class DestroyedDataset(unittest.TestCase):
    def test_the_destroyed_dataset_is_its_own_group_and_its_files_come_out(self):
        r = Rescue('modern-destroyed')
        try:
            files = [f for f in r.side['files'] if f['type'] == 'f']
            groups = api(r.port, '/api/target/groups')['groups']
            # `zfs destroy` removed the dataset from the pool, but its trees are still on the disk
            self.assertTrue(any('docs' in g['names'] for g in groups), [g['top'] for g in groups])
            rows = r.choose('docs')
            self.assertLessEqual(rows[0]['birth'], r.side['txg_before_destroy'] + 1)
            summary = r.build_catalog(rows[0])
            self.assertEqual(summary['files'], len(files))
            r.rescue(files)
        finally:
            r.close()


if __name__ == '__main__':
    unittest.main()
