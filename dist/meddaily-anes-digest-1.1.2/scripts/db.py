#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''文献日报 SQLite 溯源库

把每天抓到的文献 / 试验 / 摘要落库，之后可按任意维度回溯：
「这条结论出自哪篇文献、哪天抓的、哪次运行写的」。

表结构：
  runs          每次流水线运行一条（spec + 日期 + 阶段）
  papers        文献主表，(spec, ukey) 唯一，跨天复用；seen_count 记录出现过几次
  paper_runs    文献 <-> 运行 多对多，记录「哪次运行见到过它」
  trials        临床试验主表
  trial_runs    试验 <-> 运行 多对多
  summaries     中文摘要，(spec, date, pmid, section) 唯一

所有原始条目都存进 raw_json，将来字段变了也能还原。
'''

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# 默认库在包内 data/（发布包自洽；CLINICAL_DB 可覆盖）
DEFAULT_DB = os.environ.get('CLINICAL_DB') or os.path.join(os.path.dirname(HERE), 'data', 'digest.db')

SCHEMA = '''
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  spec TEXT NOT NULL,
  date TEXT NOT NULL,
  stage TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL DEFAULT 'running',
  params TEXT,
  counts TEXT,
  notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_spec_date ON runs(spec, date);
CREATE INDEX IF NOT EXISTS idx_runs_stage ON runs(spec, stage, date);

CREATE TABLE IF NOT EXISTS papers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  spec TEXT NOT NULL,
  ukey TEXT NOT NULL,
  pmid TEXT,
  pmcid TEXT,
  doi TEXT,
  title TEXT,
  journal TEXT,
  journal_iso TEXT,
  volume TEXT,
  issue TEXT,
  pub_date TEXT,
  pub_types TEXT,
  mesh TEXT,
  abstract TEXT,
  authors TEXT,
  is_oa INTEGER,
  in_epmc INTEGER,
  cited_by INTEGER,
  src TEXT,
  is_conference INTEGER,
  score REAL,
  section TEXT,
  diseases TEXT,
  disease_zh TEXT,
  score_reasons TEXT,
  first_seen_at TEXT,
  last_seen_at TEXT,
  seen_count INTEGER DEFAULT 1,
  raw_json TEXT,
  UNIQUE(spec, ukey)
);
CREATE INDEX IF NOT EXISTS idx_papers_pmid ON papers(pmid);
CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi);
CREATE INDEX IF NOT EXISTS idx_papers_title ON papers(title);

CREATE TABLE IF NOT EXISTS paper_runs (
  run_id INTEGER NOT NULL,
  paper_id INTEGER NOT NULL,
  date TEXT NOT NULL,
  score REAL,
  section TEXT,
  rank INTEGER,
  PRIMARY KEY (run_id, paper_id),
  FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE,
  FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_pr_paper ON paper_runs(paper_id);

CREATE TABLE IF NOT EXISTS trials (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  spec TEXT NOT NULL,
  nct TEXT NOT NULL,
  title TEXT,
  official_title TEXT,
  sponsor TEXT,
  phase TEXT,
  status TEXT,
  enrollment INTEGER,
  last_update TEXT,
  condition TEXT,
  url TEXT,
  first_seen_at TEXT,
  last_seen_at TEXT,
  seen_count INTEGER DEFAULT 1,
  raw_json TEXT,
  UNIQUE(spec, nct)
);

CREATE TABLE IF NOT EXISTS trial_runs (
  run_id INTEGER NOT NULL,
  trial_id INTEGER NOT NULL,
  date TEXT NOT NULL,
  PRIMARY KEY (run_id, trial_id),
  FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE,
  FOREIGN KEY (trial_id) REFERENCES trials(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS summaries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  spec TEXT NOT NULL,
  date TEXT NOT NULL,
  run_id INTEGER,
  ukey TEXT NOT NULL DEFAULT '',
  pmid TEXT,
  section TEXT,
  score REAL,
  title TEXT,
  journal TEXT,
  journal_iso TEXT,
  pub_date TEXT,
  doi TEXT,
  disease_zh TEXT,
  pub_types TEXT,
  summary TEXT,
  summary_ok INTEGER,
  model TEXT,
  raw_json TEXT,
  UNIQUE(spec, date, ukey)
);
CREATE INDEX IF NOT EXISTS idx_sum_pmid ON summaries(pmid);


'''

TABLE_ORDER = ('runs', 'papers', 'paper_runs', 'trials', 'trial_runs', 'summaries')


def now():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def j(v):
    return json.dumps(v, ensure_ascii=False)


def connect(db_path=None, init=True):
    db_path = db_path or DEFAULT_DB
    d = os.path.dirname(db_path)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    if init:
        conn.executescript(SCHEMA)
        conn.commit()
    return conn


def upsert(conn, table, keys, data, keep=()):
    '''INSERT ... ON CONFLICT DO UPDATE，列名自动对齐，杜绝占位符数量错配。'''
    cols = list(keys) + [k for k in data.keys() if k not in keys]
    ph = ','.join(['?'] * len(cols))
    upd = [c for c in cols if c not in keys and c not in keep]
    set_clause = ','.join('%s=excluded.%s' % (c, c) for c in upd)
    sql = 'INSERT INTO %s (%s) VALUES (%s) ON CONFLICT(%s) DO UPDATE SET %s' % (
        table, ','.join(cols), ph, ','.join(keys), set_clause)
    args = tuple(data.get(c) for c in cols)
    conn.execute(sql, args)
    return conn.execute('SELECT id FROM %s WHERE %s LIMIT 1' % (table, ' AND '.join(k + '=?' for k in keys)),
                        tuple(keys and [data.get(k) for k in keys])).fetchone()['id']


def paper_ukey(p):
    pmid = str(p.get('pmid') or '').strip()
    if pmid:
        return 'pmid:' + pmid
    doi = (p.get('doi') or '').strip().lower()
    if doi:
        return 'doi:' + doi
    raw = '|'.join([str(p.get('title') or ''), str(p.get('journal') or ''), str(p.get('pub_date') or '')])
    return 'sha1:' + hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]


def begin_run(conn, spec, date, stage, params=None):
    # 同日同科同阶段重复运行（如 finalize 修正后重跑）：复用最近一次 run，
    # 避免 runs/paper_runs 记录重复膨胀（paper_runs 主键 (run_id,paper_id) 覆盖更新）
    row = conn.execute(
        'SELECT id FROM runs WHERE spec=? AND date=? AND stage=? ORDER BY id DESC LIMIT 1',
        (spec, date, stage)).fetchone()
    if row:
        conn.execute(
            'UPDATE runs SET started_at=?, finished_at=NULL, status=?, params=?, counts=NULL WHERE id=?',
            (now(), 'running', j(params or {}), row[0]))
        conn.commit()
        return row[0]
    cur = conn.execute(
        'INSERT INTO runs (spec,date,stage,started_at,status,params) VALUES (?,?,?,?,?,?)',
        (spec, date, stage, now(), 'running', j(params or {})))
    conn.commit()
    return cur.lastrowid


def end_run(conn, run_id, status='ok', counts=None, notes=None):
    conn.execute('UPDATE runs SET finished_at=?, status=?, counts=?, notes=? WHERE id=?',
                 (now(), status, j(counts or {}), notes, run_id))
    conn.commit()


def upsert_paper(conn, spec, p):
    ukey = paper_ukey(p)
    data = {
        'spec': spec, 'ukey': ukey,
        'pmid': p.get('pmid'), 'pmcid': p.get('pmcid'), 'doi': p.get('doi'),
        'title': p.get('title'), 'journal': p.get('journal'), 'journal_iso': p.get('journal_iso'),
        'volume': p.get('volume'), 'issue': p.get('issue'), 'pub_date': p.get('pub_date'),
        'pub_types': j(p.get('pub_types') or []), 'mesh': j(p.get('mesh') or []),
        'abstract': p.get('abstract'), 'authors': j(p.get('authors') or []),
        'is_oa': 1 if p.get('is_oa') else 0,
        'in_epmc': 1 if p.get('in_epmc') else 0,
        'cited_by': p.get('cited_by'), 'src': p.get('src'),
        'is_conference': 1 if p.get('is_conference') else 0,
        'score': p.get('score'), 'section': p.get('section'),
        'diseases': j(p.get('diseases') or []), 'disease_zh': j(p.get('disease_zh') or []),
        'score_reasons': j(p.get('score_reasons') or []),
        'raw_json': j(p),
        'first_seen_at': now(), 'last_seen_at': now(),
    }
    pid = upsert(conn, 'papers', ('spec', 'ukey'), data, keep=('first_seen_at',))
    conn.execute('UPDATE papers SET seen_count=seen_count+1, last_seen_at=? WHERE id=?', (now(), pid))
    return pid


def upsert_trial(conn, spec, t):
    nct = (t.get('nct') or '').strip()
    if not nct:
        return None
    data = {
        'spec': spec, 'nct': nct,
        'title': t.get('title'), 'official_title': t.get('official_title'),
        'sponsor': t.get('sponsor'), 'phase': t.get('phase'), 'status': t.get('status'),
        'enrollment': t.get('enrollment'), 'last_update': t.get('last_update'),
        'condition': t.get('condition'), 'url': t.get('url'), 'raw_json': j(t),
        'first_seen_at': now(), 'last_seen_at': now(),
    }
    tid = upsert(conn, 'trials', ('spec', 'nct'), data, keep=('first_seen_at',))
    conn.execute('UPDATE trials SET seen_count=seen_count+1, last_seen_at=? WHERE id=?', (now(), tid))
    return tid


def ingest_papers(conn, spec, date, payload, run_id=None):
    n_p = 0
    for idx, p in enumerate(payload.get('papers') or []):
        pid = upsert_paper(conn, spec, p)
        if run_id:
            conn.execute('INSERT OR REPLACE INTO paper_runs (run_id,paper_id,date,score,section,rank)'
                         ' VALUES (?,?,?,?,?,?)',
                         (run_id, pid, date, p.get('score'), p.get('section'), idx + 1))
        n_p += 1
    n_t = 0
    for t in (payload.get('trials') or []):
        tid = upsert_trial(conn, spec, t)
        if tid is not None and run_id:
            conn.execute('INSERT OR REPLACE INTO trial_runs (run_id,trial_id,date) VALUES (?,?,?)',
                         (run_id, tid, date))
        if tid is not None:
            n_t += 1
    conn.commit()
    return {'papers': n_p, 'trials': n_t}


def ingest_summaries(conn, spec, date, payload, run_id=None):
    items = list(payload.get('papers') or [])
    n = 0
    for s in items:
        data = {
            'spec': spec, 'date': date, 'run_id': run_id,
            'ukey': paper_ukey(s),
            'pmid': str(s.get('pmid') or '').strip(), 'section': s.get('section') or '',
            'score': s.get('score'), 'title': s.get('title'), 'journal': s.get('journal'),
            'journal_iso': s.get('journal_iso'), 'pub_date': s.get('pub_date'), 'doi': s.get('doi'),
            'disease_zh': j(s.get('disease_zh') or []), 'pub_types': j(s.get('pub_types') or []),
            'summary': s.get('summary'),
            'summary_ok': 1 if s.get('summary_ok') else 0,
            'model': payload.get('model'), 'raw_json': j(s),
        }
        upsert(conn, 'summaries', ('spec', 'date', 'ukey'), data)
        n += 1
    conn.commit()
    return {'summaries': n}


def cmd_init(args):
    conn = connect(args.db)
    print('✅ 数据库就绪: ' + (args.db or DEFAULT_DB))
    for t in TABLE_ORDER:
        n = conn.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0]
        print('   %-14s %d 行' % (t, n))
    conn.close()


def cmd_ingest(args):
    conn = connect(args.db)
    run_id = args.run_id
    owned = False
    if not run_id:
        run_id = begin_run(conn, args.spec, args.date, args.stage or 'ingest',
                           {'papers': args.papers, 'summaries': args.summaries})
        owned = True
    total = {}
    if args.papers:
        with open(args.papers, encoding='utf-8') as f:
            total.update(ingest_papers(conn, args.spec, args.date, json.load(f), run_id))
    if args.summaries:
        with open(args.summaries, encoding='utf-8') as f:
            total.update(ingest_summaries(conn, args.spec, args.date, json.load(f), run_id))
    if owned:
        end_run(conn, run_id, 'ok', total)
    print('✅ 入库完成 run_id=%s %s' % (run_id, j(total)))
    conn.close()


def _require_db(args):
    db = args.db or DEFAULT_DB
    if not os.path.exists(db):
        print('❌ 数据库不存在: %s（先运行 init，或先跑一次 plan/finalize）' % db)
        sys.exit(1)


def cmd_stats(args):
    _require_db(args)
    conn = connect(args.db, init=False)
    print('=== 表行数 ===')
    for t in TABLE_ORDER:
        n = conn.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0]
        print('   %-14s %d' % (t, n))
    print('')
    print('=== 各科室 ===')
    for r in conn.execute('SELECT spec, COUNT(*) n, MIN(date) d1, MAX(date) d2 FROM runs GROUP BY spec'):
        print('   %-14s runs=%-4d %s ~ %s' % (r['spec'], r['n'], r['d1'], r['d2']))
    conn.close()


def cmd_show(args):
    _require_db(args)
    conn = connect(args.db, init=False)
    n = conn.execute('SELECT COUNT(*) c FROM paper_runs pr JOIN runs r ON r.id=pr.run_id'
                     ' WHERE r.spec=? AND pr.date=?', (args.spec, args.date)).fetchone()['c']
    print('=== %s %s 本次运行抓到 %d 篇 ===' % (args.spec, args.date, n))
    for r in conn.execute(
            'SELECT p.id, p.pmid, p.score, p.section, p.title, p.disease_zh'
            ' FROM paper_runs pr JOIN papers p ON p.id=pr.paper_id'
            ' WHERE pr.date=? AND p.spec=? ORDER BY p.score DESC LIMIT ?',
            (args.date, args.spec, args.limit)):
        print('   id=%-5s %-9s %-8s %s' % (r['id'], (r['pmid'] or '-')[:9], r['section'] or '-',
                                          (r['title'] or '')[:58]))
    conn.close()


def cmd_trace(args):
    _require_db(args)
    conn = connect(args.db, init=False)
    if args.pmid:
        rows = conn.execute('SELECT * FROM papers WHERE pmid=?', (args.pmid,)).fetchall()
        if not rows:
            print('❌ 没有 pmid=' + args.pmid + ' 的记录')
            conn.close()
            return
        for p in rows:
            print('=== pmid %s (id=%d, spec=%s) ===' % (p['pmid'], p['id'], p['spec']))
            print('   标题 : ' + str(p['title'])[:78])
            print('   期刊 : %s  (%s)' % (p['journal'], p['pub_date']))
            print('   DOI  : ' + str(p['doi']))
            print('   病种 : ' + str(p['disease_zh']))
            print('   出现 : %d 次（%s ~ %s）' % (p['seen_count'], p['first_seen_at'], p['last_seen_at']))
            print('   出现在这些运行：')
            for rr in conn.execute('SELECT r.date, r.stage, r.status FROM paper_runs pr'
                                   ' JOIN runs r ON r.id=pr.run_id WHERE pr.paper_id=? ORDER BY r.date',
                                   (p['id'],)):
                print('      %s  %-10s %s' % (rr['date'], rr['stage'], rr['status']))
            for s in conn.execute('SELECT date, section, summary FROM summaries WHERE pmid=? ORDER BY date',
                                  (args.pmid,)):
                print('   摘要 %s / %s:' % (s['date'], s['section']))
                print('      ' + str(s['summary'])[:280].replace(chr(10), ' '))
    elif args.nct:
        rows = conn.execute('SELECT * FROM trials WHERE nct=?', (args.nct,)).fetchall()
        if not rows:
            print('❌ 没有 nct=' + args.nct + ' 的记录')
        for r in rows:
            print('=== %s ===' % args.nct)
            for k in ('spec', 'title', 'sponsor', 'phase', 'status', 'enrollment',
                      'last_update', 'condition', 'url', 'seen_count'):
                print('   %-13s %s' % (k, r[k]))
    conn.close()


def main():
    ap = argparse.ArgumentParser(description='文献日报溯源库')
    ap.add_argument('--db', default=None, help='数据库路径（默认 data/digest.db）')
    sub = ap.add_subparsers(dest='cmd')

    p1 = sub.add_parser('init')
    p1.set_defaults(func=cmd_init)

    p2 = sub.add_parser('ingest')
    p2.add_argument('--spec', required=True)
    p2.add_argument('--date', required=True)
    p2.add_argument('--papers', default=None)
    p2.add_argument('--summaries', default=None)
    p2.add_argument('--run-id', type=int, default=None)
    p2.add_argument('--stage', default=None)
    p2.set_defaults(func=cmd_ingest)

    p3 = sub.add_parser('stats')
    p3.set_defaults(func=cmd_stats)

    p4 = sub.add_parser('show')
    p4.add_argument('--spec', required=True)
    p4.add_argument('--date', required=True)
    p4.add_argument('--limit', type=int, default=20)
    p4.set_defaults(func=cmd_show)

    p5 = sub.add_parser('trace')
    g = p5.add_mutually_exclusive_group(required=True)
    g.add_argument('--pmid', default=None)
    g.add_argument('--nct', default=None)
    p5.set_defaults(func=cmd_trace)

    args = ap.parse_args()
    if not getattr(args, 'func', None):
        ap.print_help()
        return 1
    args.func(args)
    return 0


if __name__ == '__main__':
    sys.exit(main())
