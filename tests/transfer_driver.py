"""Helper for tests/test_transfer_verify.py: runs transfer.py operations in a fresh process, so that
"the server was restarted" is a real restart. Environment as for the tools (state, catalog, fake ssh).

  transfer_driver.py prepare <kind> <offset> <comp> <psize> <lsize>   build the catalog of a state, select everything
  transfer_driver.py run <type> <dest> [kill-after-files N]   start a transfer, wait, print its status
  transfer_driver.py verify|recopy <type> <dest>             run the step synchronously, print the result
  transfer_driver.py donecount <type> <dest>                 how many manifest files count as copied
"""
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'scripts'))
import common  # noqa: E402
import transfer  # noqa: E402


def target(kind, dest):
    return transfer.normalize({'type': kind, 'dest': dest})


def prepare(kind, offset, comp, psize, lsize):
    os.makedirs(common.CATALOG, exist_ok=True)
    cand = ['OBJSET' if kind == 'OBJSET' else 'IND', offset, comp, psize, lsize]
    cmd = common.reader_cmd('tree', *map(str, cand))
    tsv = os.path.join(common.CATALOG, 'tree.tsv')
    with open(tsv, 'w') as f:
        subprocess.run(['bash', '-c', cmd], stdout=f, check=True)
    subprocess.run([sys.executable, '-I', os.path.join(HERE, '..', 'scripts', 'catalog.py'), tsv, '1'], check=True,
                   capture_output=True)
    db = sqlite3.connect(common.DB)
    db.execute("update entry set sel=1 where kind in ('f','l')")
    db.commit()
    with open(os.path.join(common.CATALOG, 'state.json'), 'w') as f:
        json.dump({'txg': 1, 'kind': kind, 'offset': offset, 'comp': comp, 'psize': int(psize), 'lsize': int(lsize)}, f)
    print(db.execute("select count(*) from entry where sel=1 and kind='f'").fetchone()[0])


def main():
    cmd = sys.argv[1]
    if cmd == 'prepare':
        return prepare(*sys.argv[2:])
    t = target(sys.argv[2], sys.argv[3])
    if cmd == 'run':
        kill_after = int(sys.argv[4]) if len(sys.argv) > 4 else 0
        j = transfer.Job(t)
        transfer.JOBS[j.id] = j
        if kill_after:
            original = j.write_status

            def crashing_write_status():         # runs inside the job thread, every 5 files
                original()
                if j.status['files'] >= kill_after:
                    for p in j.proc:
                        p.kill()
                    os._exit(7)                  # the whole "server" dies without any clean-up: no END marker
            j.write_status = crashing_write_status
        threading.Thread(target=j.run, daemon=True).start()
        time.sleep(0.5)
        while j.status['state'] == 'running':
            time.sleep(0.2)
        while (j.status.get('verify') or {}).get('state') == 'running':
            time.sleep(0.2)
        print(json.dumps(transfer.public(j.status)))
    elif cmd == 'donecount':
        print(len(transfer.job_for(transfer.target_id(t)).done_paths()))
    elif cmd in ('verify', 'recopy'):
        j = transfer.job_for(transfer.target_id(t))
        (j.verify if cmd == 'verify' else j.recopy)()
        print(json.dumps(transfer.public(j.status)))


main()
