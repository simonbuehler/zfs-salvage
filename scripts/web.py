#!/usr/bin/env python3
"""zfs-salvage web UI: http://localhost:8765

  /          step 1: status page (regenerated every 20 s by status_page.py in a background thread)
  /select    step 2: choose files (scripts/ui/index.html)
  /transfer  step 3: copy the selection to a target (scripts/ui/transfer.html)

Reads catalog/catalog.sqlite (catalog.py). The selection is stored in column entry.sel
(survives restarts); every change is also logged to catalog/selection_rules.jsonl.
The catalog can be rebuilt from the newest verified state with one click (POST /api/catalog/rebuild).
"""
import hmac
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jobs  # noqa: E402
import setup_api  # noqa: E402
import status_page  # noqa: E402
import transfer  # noqa: E402
import common  # noqa: E402
from common import (CANDIDATES, CATALOG, DB, LOGS, REPO, STATUS, TREE_GROUPS, WORKFLOW,  # noqa: E402
                    reader_cmd)

RULES = f'{CATALOG}/selection_rules.jsonl'
UI = f'{REPO}/scripts/ui/index.html'
STATUS_PAGE = f'{STATUS}/index.html'
PORT = int(os.environ.get('SALVAGE_UI_PORT', 8765))

# The server usually runs as root and can copy files anywhere: nothing else on this machine, and no web page the
# user happens to visit, may talk to it. Every request needs the per-session token (cookie or X-ZS-Token header; the
# launcher opens /?t=<token>, which sets the cookie), the Host must be this machine (DNS rebinding) and an Origin,
# if the browser sends one, must be this page (cross-site requests).
TOKEN = os.environ.get('ZFS_SALVAGE_TOKEN') or secrets.token_urlsafe(24)
COOKIE = f'zs_token_{PORT}'                      # cookies are not separated by port: one name per port
ALLOWED_HOSTS = {f'localhost:{PORT}', f'127.0.0.1:{PORT}', f'[::1]:{PORT}'}
ALLOWED_ORIGINS = {f'http://{h}' for h in ALLOWED_HOSTS}


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def has_catalog():
    """True once catalog.py has built the catalog (an empty or missing database file does not count)."""
    if not os.path.exists(DB):
        return False
    try:
        with sqlite3.connect(f'file:{DB}?mode=ro', uri=True) as c:
            return c.execute("select count(*) from sqlite_master where name = 'entry'").fetchone()[0] == 1
    except sqlite3.Error:
        return False


def ensure_schema():
    if not has_catalog():
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
    """List the tree of a verified state and update the catalog (keeps the selection)."""
    global CATALOG_JOB
    if CATALOG_JOB.get('state') == 'running':
        return CATALOG_JOB
    if cand is None:
        cands = json.load(open(CANDIDATES))
        best = next((c for c in cands if c.get('candidate')), None)
        if not best:
            return {'state': 'failed', 'info': 'no verified candidate with read parameters yet'}
        cand = dict(best['candidate'], txg=best['birth'])
    CATALOG_JOB = {'state': 'running', 'txg': cand['txg'], 'start': time.time(), 'info': 'listing the tree'}

    def run():
        global CATALOG_JOB
        try:
            args = f"{cand['kind']} {cand['offset']} {cand['comp']} {cand['psize']} {cand['lsize']}"
            os.makedirs(f'{LOGS}/tree', exist_ok=True)
            tsv = f"{LOGS}/tree/tree_{cand['txg']}.tsv"
            with open(tsv, 'w') as out:
                r = subprocess.run(['bash', '-c', reader_cmd('tree', args)],
                                   stdout=out, stderr=subprocess.PIPE, text=True, timeout=7200)
            if r.returncode != 0:
                raise RuntimeError(r.stderr[-300:])
            CATALOG_JOB['info'] = 'updating the catalog'
            res = subprocess.run([sys.executable, '-I', f'{REPO}/scripts/catalog.py', tsv, str(cand['txg'])],
                                 capture_output=True, text=True)
            if res.returncode != 0:
                raise RuntimeError(res.stderr[-300:])
            json.dump({'txg': cand['txg'], 'kind': cand['kind'], 'offset': cand['offset'], 'comp': cand['comp'],
                       'psize': cand['psize'], 'lsize': cand['lsize']}, open(f'{CATALOG}/state.json', 'w'))
            refresh_status()
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
    if has_catalog():
        c = db()
        try:
            cat_txg = int(c.execute("select state from entry where path='/'").fetchone()[0])
        except Exception:
            pass
        f, b = c.execute("select count(*), coalesce(sum(size),0) from entry where kind='f' and sel=1").fetchone()
        w['selection'] = {'files': f, 'bytes': b}
    newest = (w.get('newest') or {}).get('txg')
    w['catalog'] = {'txg': cat_txg, 'update': bool(newest and cat_txg and newest > cat_txg), 'job': CATALOG_JOB}
    tjobs = transfer.status()
    w['transfer'] = {'jobs': len(tjobs), 'running': any(j['state'] == 'running' for j in tjobs),
                     'last_label': tjobs[-1].get('label') if tjobs else None,
                     'done': any(j['state'] == 'done' and (j.get('verify') or {}).get('state') == 'done'
                                 and not j['verify'].get('bad_n') for j in tjobs),     # copied AND verified
                     'unverified': sum(1 for j in tjobs if j['state'] == 'done'
                                       and (j.get('verify') or {}).get('state') != 'done'),
                     'with_problems': sum(1 for j in tjobs if (j.get('verify') or {}).get('bad_n')),
                     'files': sum(j.get('files', 0) for j in tjobs),
                     'last_state': tjobs[-1]['state'] if tjobs else None}
    w['setup'] = {'case': bool(common.CASE), 'scan': setup_api.SCAN_JOB.st.get('state'), 'rank': setup_api.RANK_JOB.st.get('state')}
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

    def guard(self, u):
        """True if the request may be served; otherwise the answer (403, or the redirect that sets the cookie) is sent."""
        if self.headers.get('Host', '') not in ALLOWED_HOSTS:
            self.send({'error': 'unexpected Host header'}, 403)
            return False
        origin = self.headers.get('Origin')
        if origin is not None and origin not in ALLOWED_ORIGINS:
            self.send({'error': 'foreign Origin'}, 403)
            return False
        cookie = SimpleCookie(self.headers.get('Cookie', '')).get(COOKIE)
        supplied = self.headers.get('X-ZS-Token') or (cookie.value if cookie else '')
        if hmac.compare_digest(supplied.encode(), TOKEN.encode()):
            return True
        q = parse_qs(u.query)
        if self.command == 'GET' and hmac.compare_digest(q.get('t', [''])[0].encode(), TOKEN.encode()):
            rest = urlencode([(k, v) for k, vs in q.items() if k != 't' for v in vs])
            self.send_response(302)
            self.send_header('Location', u.path + (f'?{rest}' if rest else ''))
            self.send_header('Set-Cookie', f'{COOKIE}={TOKEN}; Path=/; HttpOnly; SameSite=Strict')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return False
        self.send({'error': 'forbidden: open the link that zfs-salvage printed (it ends in ?t=…)'}, 403)
        return False

    def do_GET(self):
        u = urlparse(self.path)
        if not self.guard(u):
            return
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path in ('/', '/index.html', '/status'):
            if os.path.exists(STATUS_PAGE):
                return self.send(open(STATUS_PAGE, 'rb').read(), ctype='text/html')
            return self.send(b'<meta http-equiv="refresh" content="5">Status page is being generated ...', ctype='text/html')
        if u.path in ('/select', '/auswahl'):
            return self.send(open(UI, 'rb').read(), ctype='text/html')
        if u.path == '/setup':
            return self.send(open(f'{REPO}/scripts/ui/setup.html', 'rb').read(), ctype='text/html')
        if u.path == '/api/setup/state':
            return self.send(setup_api.state())
        if u.path == '/api/setup/doctor':
            return self.send(setup_api.run_doctor())
        if u.path == '/api/setup/devices':
            return self.send(setup_api.devices(q.get('extra', '').split('\n')))
        if u.path == '/transfer':
            return self.send(open(f'{REPO}/scripts/ui/transfer.html', 'rb').read(), ctype='text/html')
        if u.path == '/api/transfer/preset':                    # optional prepared target
            f = f'{CATALOG}/transfer_preset.json'
            return self.send(json.load(open(f)) if os.path.exists(f) else {})
        if u.path == '/api/transfer/hosts':
            return self.send(transfer.ssh_hosts())
        if u.path == '/api/transfer/places':
            return self.send(transfer.places(q.get('type', 'local'), q.get('host', '')))
        if u.path == '/steps.js':
            return self.send(open(f'{REPO}/scripts/ui/steps.js', 'rb').read(), ctype='application/javascript')
        if u.path == '/api/target/groups':
            try:
                with open(TREE_GROUPS) as f:
                    return self.send(json.load(f))
            except (OSError, ValueError):
                return self.send({'groups': [], 'needs_choice': False})
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
        if not has_catalog():
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
        if not self.guard(urlparse(self.path)):
            return
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
        if self.path == '/api/catalog/rebuild':
            return self.send(rebuild_catalog(body.get('candidate')))
        if self.path == '/api/setup/readonly':
            return self.send(setup_api.make_read_only(str(body.get('dev', ''))))
        if self.path == '/api/setup/probe':
            return self.send(setup_api.probe(body))
        if self.path == '/api/setup/case':
            return self.send(setup_api.create_case(body))
        if self.path in ('/api/jobs/probe/cancel', '/api/jobs/case/cancel'):
            return self.send(setup_api.cancel(self.path.split('/')[3]))
        if self.path == '/api/jobs/scan/start':
            return self.send(setup_api.start_scan(body))
        if self.path == '/api/jobs/scan/pause':
            return self.send(setup_api.pause_scan())
        if self.path == '/api/target/choose':
            return self.send(*choose_target(body))
        if self.path == '/api/transfer/check':
            return self.send(transfer.check(body))
        if self.path == '/api/transfer/start':
            return self.send(transfer.start(body))
        if self.path == '/api/transfer/verify':
            return self.send(transfer.start_verify(body.get('id', '')))
        if self.path == '/api/transfer/recopy':
            return self.send(transfer.start_recopy(body.get('id', '')))
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


def choose_target(body):
    """The user clicked "this one" on a group of trees (tree_groups.json): remember its names in the case, relabel the
    candidates (no new reads) and regenerate the status page."""
    names = body.get('names')
    if not (isinstance(names, list) and names and all(isinstance(n, str) and n for n in names)):
        return {'error': 'names must be a non-empty list'}, 400
    try:
        common.set_root_names(names[:100])
    except (ValueError, OSError) as e:
        return {'error': str(e)}, 400
    subprocess.run([sys.executable, '-I', f'{REPO}/scripts/rank-trees.py', '0'], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=600)
    refresh_status()                           # the page the browser reloads next shows the choice already
    return {'ok': True, 'names': names[:100]}, 200


STATUS_LOCK = threading.Lock()


def refresh_status():
    """Regenerate the status page now (errors go to logs/status_page.err)."""
    with STATUS_LOCK:
        try:
            status_page.main()
        except Exception as e:  # noqa: BLE001 - the page must not take the server down
            os.makedirs(LOGS, exist_ok=True)
            with open(f'{LOGS}/status_page.err', 'a') as f:
                f.write(time.strftime('%H:%M:%S ') + f'{type(e).__name__}: {e}\n')


def status_loop():
    """Regenerate the status page every 20 s, and right away after a choice, a catalog or a finished job."""
    while True:
        refresh_status()
        time.sleep(20)


def restart_when_a_case_was_written():
    """Many settings are read from the case file when the modules are imported: after the setup page wrote a new case,
    web.py starts again (same process, same port, same token) instead of running with the old values."""
    while not setup_api.RESTART['pending']:
        time.sleep(0.5)
    time.sleep(1)                                     # let the last status request finish
    jobs.shutdown()
    os.environ['ZFS_SALVAGE_TOKEN'] = TOKEN
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, '-I', os.path.abspath(__file__)])


def stop(*_):
    jobs.shutdown()
    sys.exit(0)


if __name__ == '__main__':
    import signal
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    ensure_schema()
    setup_api.ON_CHANGE = refresh_status
    setup_api.resume()
    threading.Thread(target=restart_when_a_case_was_written, daemon=True).start()
    threading.Thread(target=status_loop, daemon=True).start()
    print(f'zfs-salvage web UI: http://localhost:{PORT}/?t={TOKEN}   (open this link; others are refused)', file=sys.stderr)
    ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()
