#!/usr/bin/env python3
"""zfs-salvage web UI: http://localhost:8765

  /          step 1: status page (regenerated every 20 s by status_page.py in a background thread)
  /select    step 2: choose files (scripts/ui/index.html)
  /transfer  step 3: copy the selection to a target (scripts/ui/transfer.html)

Reads catalog/catalog.sqlite (catalog.py). The selection is stored in column entry.sel
(survives restarts); every change is also logged to catalog/selection_rules.jsonl.
The catalog can be rebuilt from the newest verified state with one click (POST /api/catalog/rebuild).
"""
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import status_page  # noqa: E402
import transfer  # noqa: E402
from common import CANDIDATES, DB, HOST, REPO, WORKFLOW, reader_cmd  # noqa: E402

RULES = f'{REPO}/catalog/selection_rules.jsonl'
UI = f'{REPO}/scripts/ui/index.html'
STATUS_PAGE = f'{REPO}/status/index.html'
PORT = int(os.environ.get('SALVAGE_UI_PORT', 8765))


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def ensure_schema():
    if not os.path.exists(DB):
        return
    c = db()
    cols = {r[1] for r in c.execute('pragma table_info(entry)')}
    if 'sel' not in cols:
        c.execute('alter table entry add column sel integer default 0')
        c.commit()


def like_prefix(path):
    p = path.rstrip('/') + '/'
    return p.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


def filter_sql(f):
    where, args = ["kind = 'f'"], []
    if f.get('q'):
        where.append('lower(path) like ?')
        args.append('%' + f['q'].lower() + '%')
    if f.get('cat'):
        cats = [x for x in f['cat'].split(',') if x]
        where.append(f"cat in ({','.join('?' * len(cats))})")
        args += cats
    if f.get('from'):
        where.append('mtime >= ?')
        args.append(f['from'])
    if f.get('to'):
        where.append('mtime <= ?')
        args.append(f['to'] + ' 99')
    if f.get('min'):
        where.append('size >= ?')
        args.append(int(float(f['min']) * 1e6))
    if f.get('nohidden') in ('1', 1, True, 'true'):
        where.append("path not like '%/.%'")                 # skip hidden files and folders (.cache, .recycle ...)
    if f.get('under') and f['under'] != '/':
        where.append("path like ? escape '\\'")
        args.append(like_prefix(f['under']))
    return ' and '.join(where), args


def summary(c):
    tot = c.execute("select count(*), coalesce(sum(size),0) from entry where kind='f'").fetchone()
    sel = c.execute("select count(*), coalesce(sum(size),0) from entry where kind='f' and sel=1").fetchone()
    cats = [dict(r) for r in c.execute(
        "select cat, count(*) n, coalesce(sum(size),0) s, sum(sel) nsel, coalesce(sum(case when sel=1 then size end),0) ssel "
        "from entry where kind='f' group by cat order by s desc")]
    bad = c.execute("select count(*) from entry where status != 'ok'").fetchone()[0]
    state = c.execute("select state from entry where path='/'").fetchone()[0]
    return {'files': tot[0], 'bytes': tot[1], 'sel_files': sel[0], 'sel_bytes': sel[1],
            'cats': cats, 'unreadable': bad, 'state': state, 'catalog_job': CATALOG_JOB}


def children(c, path):
    rows = c.execute("select path,name,kind,size,mtime,cat,status,tot_size,tot_files,sel from entry "
                     "where parent=? and path != '/' order by kind != 'd', lower(name)", (path,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if r['kind'] == 'd':
            d['sel_files'] = c.execute("select count(*) from entry where kind='f' and sel=1 and path like ? escape '\\'",
                                       (like_prefix(r['path']),)).fetchone()[0]
        out.append(d)
    return out


def selstate(c, paths):
    """Checkbox state per path: files 0/1, folders 'all' / 'part' / 0."""
    out = {}
    for p in paths[:2000]:
        r = c.execute('select kind, sel, tot_files from entry where path=?', (p,)).fetchone()
        if not r:
            continue
        if r['kind'] != 'd':
            out[p] = r['sel'] or 0
            continue
        n = c.execute("select count(*) from entry where kind='f' and sel=1 and path like ? escape '\\'",
                      (like_prefix(p),)).fetchone()[0]
        out[p] = 0 if n == 0 else ('all' if n >= (r['tot_files'] or 0) else 'part')
    return out


def apply(c, body):
    op = 1 if body.get('op') == 'add' else 0
    if 'path' in body:
        p = body['path']
        c.execute("update entry set sel=? where kind='f' and (path=? or path like ? escape '\\')", (op, p, like_prefix(p)))
    elif 'paths' in body:                                       # a prepared list of files
        c.executemany("update entry set sel=? where kind='f' and path=?", [(op, p) for p in body['paths']])
    elif 'filter' in body:
        w, a = filter_sql(body['filter'])
        c.execute(f'update entry set sel=? where {w}', [op] + a)
    elif body.get('op') == 'reset':
        c.execute('update entry set sel=0')
    c.commit()
    rule = dict(body)
    if 'paths' in rule:                                         # log the size of a list, not the list
        rule['paths'] = f"{len(rule['paths'])} paths" + (f" from {body.get('source')}" if body.get('source') else '')
    with open(RULES, 'a') as f:
        f.write(json.dumps({'t': time.strftime('%Y-%m-%d %H:%M:%S'), **rule}, ensure_ascii=False) + '\n')


# ---------------------------------------------------------------- catalog rebuild
CATALOG_JOB = {'state': 'idle'}


def rebuild_catalog(cand=None):
    """List the tree of a verified state on the recovery host and update the catalog (keeps the selection)."""
    global CATALOG_JOB
    if CATALOG_JOB.get('state') == 'running':
        return CATALOG_JOB
    if cand is None:
        cands = json.load(open(CANDIDATES))
        best = next((c for c in cands if c.get('candidate')), None)
        if not best:
            return {'state': 'failed', 'info': 'no verified candidate with read parameters yet'}
        cand = dict(best['candidate'], txg=best['birth'])
    CATALOG_JOB = {'state': 'running', 'txg': cand['txg'], 'start': time.time(), 'info': 'listing the tree on the recovery host'}

    def run():
        global CATALOG_JOB
        try:
            args = f"{cand['kind']} {cand['offset']} {cand['comp']} {cand['psize']} {cand['lsize']}"
            os.makedirs(f'{REPO}/logs/tree', exist_ok=True)
            tsv = f"{REPO}/logs/tree/tree_{cand['txg']}.tsv"
            with open(tsv, 'w') as out:
                r = subprocess.run(['ssh', '-o', 'ServerAliveInterval=30', HOST, reader_cmd('tree', args)],
                                   stdout=out, stderr=subprocess.PIPE, text=True, timeout=7200)
            if r.returncode != 0:
                raise RuntimeError(r.stderr[-300:])
            CATALOG_JOB['info'] = 'updating the catalog'
            res = subprocess.run([sys.executable, '-I', f'{REPO}/scripts/catalog.py', tsv, str(cand['txg'])],
                                 capture_output=True, text=True)
            if res.returncode != 0:
                raise RuntimeError(res.stderr[-300:])
            json.dump({'txg': cand['txg'], 'kind': cand['kind'], 'offset': cand['offset'], 'comp': cand['comp'],
                       'psize': cand['psize'], 'lsize': cand['lsize']}, open(f'{REPO}/catalog/state.json', 'w'))
            CATALOG_JOB = {'state': 'done', 'txg': cand['txg'], 'end': time.time(), 'info': res.stdout.splitlines()[0]}
        except Exception as e:  # noqa: BLE001
            CATALOG_JOB = {'state': 'failed', 'txg': cand.get('txg'), 'info': f'{type(e).__name__}: {e}'}
    threading.Thread(target=run, daemon=True).start()
    return CATALOG_JOB


def workflow():
    """State of the four workflow steps (scan, catalog, select, transfer) for the step bar."""
    w = {}
    try:
        w.update(json.load(open(WORKFLOW)))     # written by status_page.py
    except Exception:
        w['scan'] = {}
    cat_txg = None
    if os.path.exists(DB):
        c = db()
        try:
            cat_txg = int(c.execute("select state from entry where path='/'").fetchone()[0])
        except Exception:
            pass
        f, b = c.execute("select count(*), coalesce(sum(size),0) from entry where kind='f' and sel=1").fetchone()
        w['selection'] = {'files': f, 'bytes': b}
    newest = (w.get('newest') or {}).get('txg')
    w['catalog'] = {'txg': cat_txg, 'update': bool(newest and cat_txg and newest > cat_txg), 'job': CATALOG_JOB}
    jobs = transfer.status()
    w['transfer'] = {'jobs': len(jobs), 'running': any(j['state'] == 'running' for j in jobs),
                     'last_label': jobs[-1].get('label') if jobs else None,
                     'done': any(j['state'] == 'done' for j in jobs),
                     'files': sum(j.get('files', 0) for j in jobs),
                     'last_state': jobs[-1]['state'] if jobs else None}
    return w


# ---------------------------------------------------------------- HTTP
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, obj, code=200, ctype='application/json'):
        data = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype + '; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path in ('/', '/index.html', '/status'):
            if os.path.exists(STATUS_PAGE):
                return self.send(open(STATUS_PAGE, 'rb').read(), ctype='text/html')
            return self.send(b'<meta http-equiv="refresh" content="5">Status page is being generated ...', ctype='text/html')
        if u.path in ('/select', '/auswahl'):
            return self.send(open(UI, 'rb').read(), ctype='text/html')
        if u.path == '/transfer':
            return self.send(open(f'{REPO}/scripts/ui/transfer.html', 'rb').read(), ctype='text/html')
        if u.path == '/api/transfer/preset':                    # optional prepared target
            f = f'{REPO}/catalog/transfer_preset.json'
            return self.send(json.load(open(f)) if os.path.exists(f) else {})
        if u.path == '/api/transfer/hosts':
            return self.send(transfer.ssh_hosts())
        if u.path == '/api/transfer/places':
            return self.send(transfer.places(q.get('type', 'local'), q.get('host', '')))
        if u.path == '/steps.js':
            return self.send(open(f'{REPO}/scripts/ui/steps.js', 'rb').read(), ctype='application/javascript')
        if u.path == '/api/workflow':
            return self.send(workflow())
        if u.path == '/api/catalog/status':
            return self.send(CATALOG_JOB)
        if u.path == '/api/transfer/status':
            return self.send(transfer.status())
        if u.path == '/api/transfer/manifest':
            f = f"{transfer.TDIR}/{os.path.basename(q.get('id', ''))}/manifest.tsv"
            if os.path.exists(f):
                return self.send(open(f, 'rb').read(), ctype='text/tab-separated-values')
            return self.send({'error': 'no manifest'}, 404)
        if not os.path.exists(DB):
            return self.send({'error': 'no catalog yet'}, 404)
        c = db()
        if u.path == '/api/summary':
            return self.send(summary(c))
        if u.path == '/api/children':
            return self.send(children(c, q.get('path', '/')))
        if u.path == '/api/search':
            w, a = filter_sql(q)
            n, s, ns = c.execute(f'select count(*), coalesce(sum(size),0), coalesce(sum(sel),0) from entry where {w}', a).fetchone()
            items = [dict(r) for r in c.execute(
                f'select path,name,size,mtime,cat,status,sel from entry where {w} order by path limit 200 offset ?',
                a + [int(q.get('offset', 0))])]
            return self.send({'count': n, 'bytes': s, 'sel': ns, 'items': items})
        if u.path == '/api/unreadable':
            return self.send([dict(r) for r in c.execute("select path, kind, status from entry where status != 'ok'")])
        self.send({'error': 'unknown'}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
        if self.path == '/api/catalog/rebuild':
            return self.send(rebuild_catalog(body.get('candidate')))
        if self.path == '/api/transfer/check':
            return self.send(transfer.check(body))
        if self.path == '/api/transfer/start':
            return self.send(transfer.start(body))
        if self.path == '/api/transfer/stop':
            j = transfer.JOBS.get(body.get('id'))
            if j:
                j.stop()
            return self.send({'ok': bool(j)})
        if self.path == '/api/selstate':
            return self.send(selstate(db(), body.get('paths', [])))
        if self.path == '/api/select':
            c = db()
            apply(c, body)
            return self.send(summary(c))
        self.send({'error': 'unknown'}, 404)


def status_loop():
    """Regenerate the status page every 20 s (errors go to logs/status_page.err)."""
    while True:
        try:
            status_page.main()
        except Exception as e:  # noqa: BLE001 - the page must not take the server down
            with open(f'{REPO}/logs/status_page.err', 'a') as f:
                f.write(time.strftime('%H:%M:%S ') + f'{type(e).__name__}: {e}\n')
        time.sleep(20)


if __name__ == '__main__':
    ensure_schema()
    threading.Thread(target=status_loop, daemon=True).start()
    print(f'zfs-salvage web UI: http://localhost:{PORT}', file=sys.stderr)
    ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()
