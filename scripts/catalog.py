#!/usr/bin/env python3
"""Build or update catalog/catalog.sqlite from a `zsread tree` listing (TSV).

Usage: catalog.py <tree.tsv> <state_label>     e.g. catalog.py logs/tree/tree_728651.tsv 728651
TSV columns: path, type (f/d/l), size, mtime, obj, status

Re-running with a newer state keeps the user's selection (column sel), because rows are
upserted by path.
"""
import os
import sqlite3
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = f'{REPO}/catalog/catalog.sqlite'

CATS = {
    'photo': {'jpg', 'jpeg', 'png', 'gif', 'tif', 'tiff', 'heic', 'cr2', 'nef', 'arw', 'dng', 'bmp', 'webp', 'raf', 'orf'},
    'video': {'mp4', 'mov', 'avi', 'mts', 'm2ts', '3gp', 'mkv', 'wmv', 'mpg', 'mpeg', 'm4v', 'flv'},
    'audio': {'mp3', 'flac', 'ogg', 'wav', 'm4a', 'aac', 'wma'},
    'document': {'pdf', 'doc', 'docx', 'odt', 'xls', 'xlsx', 'ods', 'ppt', 'pptx', 'txt', 'rtf', 'md'},
    'archive': {'zip', 'gz', 'tgz', 'bz2', 'xz', '7z', 'rar', 'tar'},
}
EXT2CAT = {e: c for c, es in CATS.items() for e in es}


def main():
    src, state = sys.argv[1], sys.argv[2]
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    db = sqlite3.connect(DB)
    db.executescript('''
    create table if not exists entry(path text primary key, parent text, name text, kind text,
        size integer, mtime text, obj integer, state text, status text, ext text, cat text,
        tot_size integer, tot_files integer, sel integer default 0);
    create index if not exists entry_parent on entry(parent);
    create index if not exists entry_cat on entry(cat);
    create index if not exists entry_name on entry(name);
    ''')
    rows = []
    for line in open(src, encoding='utf-8', errors='surrogateescape'):
        if line.startswith('#'):
            continue
        c = line.rstrip('\n').split('\t')
        if len(c) < 6:
            continue
        path, kind, size, mtime, obj, status = c[:6]
        parent, name = path.rsplit('/', 1)
        ext = name.rsplit('.', 1)[1].lower() if '.' in name[1:] and kind == 'f' else ''
        cat = 'folder' if kind == 'd' else ('link' if kind == 'l' else EXT2CAT.get(ext, 'other'))
        rows.append((path, parent or '/', name, kind, int(size) if size.isdigit() else None, mtime,
                     int(obj) if obj.isdigit() else None, state, status, ext, cat))
    db.executemany('''insert into entry(path,parent,name,kind,size,mtime,obj,state,status,ext,cat)
        values (?,?,?,?,?,?,?,?,?,?,?)
        on conflict(path) do update set parent=excluded.parent, name=excluded.name, kind=excluded.kind,
          size=excluded.size, mtime=excluded.mtime, obj=excluded.obj, state=excluded.state,
          status=excluded.status, ext=excluded.ext, cat=excluded.cat''', rows)
    db.execute('''insert into entry(path,parent,name,kind,state,status,cat) values ('/','','/','d',?,'ok','folder')
        on conflict(path) do update set state=excluded.state''', (state,))
    # recursive totals per directory
    tot = {}
    for path, kind, size in db.execute("select path, kind, size from entry where kind != 'd'"):
        p = path
        while True:
            p = p.rsplit('/', 1)[0] or '/'
            s, n = tot.get(p, (0, 0))
            tot[p] = (s + (size or 0), n + 1)
            if p == '/':
                break
    db.executemany('update entry set tot_size=?, tot_files=? where path=?', [(s, n, p) for p, (s, n) in tot.items()])
    db.commit()
    n, s = db.execute("select count(*), sum(size) from entry where kind='f'").fetchone()
    print(f'{len(rows)} entries, {n} files, {(s or 0) / 1e9:.1f} GB')
    for cat, cn, cs in db.execute("select cat, count(*), sum(size) from entry where kind='f' group by cat order by 3 desc"):
        print(f'  {cat:10} {cn:7} {(cs or 0) / 1e9:7.1f} GB')


if __name__ == '__main__':
    main()
