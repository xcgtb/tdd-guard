# -*- coding: utf-8 -*-
"""SQLite 存储层：state/ 下全部运行状态与执行历史的唯一存储（config.json 仍是文件）。

设计约定（与全项目其它模块一致）：
- 库文件固定为 STATE_DIR/ttd-guard.db（WAL 模式）；路径随 state.STATE_DIR 惰性解析，
  测试改 STATE_DIR 后自动落到新库，不串数据（测试结束调 close_all() 释放连接）；
- 表：plans（治理计划）/ audit（执行历史）/ sub_state（追更状态）/ subscriptions（订阅列表）/
  docs（各类整份文档：快照、缓存、队列……按名字存取，带版本号）/ tmdb_cache（每个请求一行）/
  kv / dedup；
- 旧版 JSON 文件只由 db_migrate() 一次性读取导入，导入后改名为 *.migrated，业务代码不再读写；
- 错误策略：只捕获 sqlite3.Error / JSON 编解码错误 / OSError（建目录失败），
  记 WARNING（同一操作 60 秒内最多一条）后返回默认值，绝不影响治理主流程。
"""
import contextlib
import copy
import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from . import config as _cfg
from . import state

DB_NAME = 'ttd-guard.db'
SCHEMA_VERSION = '2'
PLAN_KEEP_DAYS = 7          # 计划保留 7 天（与 purge_old 一致）
DEDUP_TTL = 7 * 86400       # 去重键保留 7 天
TMDB_MAX_AGE = 86400        # TMDB 缓存最长保留 24 小时（最长 TTL = TMDB_INFO_TTL）

# docs 表文档名（整份读写的状态/缓存）
DOC_SUB_REPORT = 'subscription_report'
DOC_GOV_LATEST = 'gov_latest'
DOC_GOV_AUTO = 'gov_auto'
DOC_MANUAL_DONE = 'manual_done'
DOC_WASH_RESIDUALS = 'wash_residuals'
DOC_BOT_DELETE_QUEUE = 'bot_delete_queue'
DOC_INGEST = 'ingest_cache'
DOC_EMBY_LIB = 'emby_library_with_tmdb'
DOC_EMBY_OVERVIEW = 'emby_overview'
DOC_LIB_SNAPSHOT = 'library_snapshot'
DOC_EMBY_INDEX = 'emby_index'
DOC_STRM_COUNT = 'strm_count'

# 旧版 JSON 文件位置（仅 db_migrate 读取）：(基准目录, 文件名)，'state' = STATE_DIR，'data' = STATE_DIR.parent
LEGACY_DOC_FILES = {
    DOC_SUB_REPORT:       ('state', 'subscription_report.json'),
    DOC_GOV_LATEST:       ('state', 'gov_latest.json'),
    DOC_GOV_AUTO:         ('data', 'gov_auto.json'),
    DOC_MANUAL_DONE:      ('state', 'manual_done.json'),
    DOC_WASH_RESIDUALS:   ('state', 'wash_residuals.json'),
    DOC_BOT_DELETE_QUEUE: ('state', 'bot_delete_queue.json'),
    DOC_INGEST:           ('state', 'ingest_cache.json'),
    DOC_EMBY_LIB:         ('state', 'emby_library_with_tmdb.json'),
    DOC_EMBY_OVERVIEW:    ('state', 'emby_overview_cache.json'),
    DOC_LIB_SNAPSHOT:     ('state', 'library_snapshot.json'),
    DOC_EMBY_INDEX:       ('state', 'emby_index_cache.json'),
    DOC_STRM_COUNT:       ('state', 'strm_count_cache.json'),
}
LEGACY_SUB_STATE_FILE = ('state', 'subscriptions_state.json')
LEGACY_TMDB_FILE = ('state', 'tmdb_cache.json')
LEGACY_AUDIT_DIR = ('data', 'records')

_DBS = {}                   # path_str -> sqlite3.Connection（按路径缓存，测试换目录即换库）
_LOCK = threading.RLock()   # 进程内串行化全部库访问（可重入：事务内还会调 _conn()）
_ERRORS = (sqlite3.Error, ValueError, TypeError, OSError)
_WARN_INTERVAL = 60
_warn_last = {}             # op -> 上次告警时间（同一操作 60 秒内只告警一次）

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    state TEXT NOT NULL,
    rule_sig TEXT DEFAULT '',
    stats TEXT DEFAULT '{}',
    executed_at REAL,
    payload TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plans_ts ON plans(ts);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    ts_epoch REAL NOT NULL DEFAULT 0,
    category TEXT DEFAULT '',
    title TEXT DEFAULT '',
    details TEXT DEFAULT '[]',
    rule_sig TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);

CREATE TABLE IF NOT EXISTS sub_state (
    store TEXT NOT NULL,
    sid TEXT NOT NULL,
    payload TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (store, sid)
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT,
    ts REAL
);

CREATE TABLE IF NOT EXISTS dedup (
    k TEXT PRIMARY KEY,
    ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS docs (
    name TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    version INTEGER NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tmdb_cache (
    k TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tmdb_ts ON tmdb_cache(ts);

CREATE TABLE IF NOT EXISTS subscriptions (
    id TEXT PRIMARY KEY,
    pos INTEGER NOT NULL,
    payload TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


def _warn(op, err):
    """存储层错误统一出口：记 WARNING，同一 op 60 秒内最多一条（防止刷屏）。"""
    now = time.time()
    with _LOCK:
        if now - _warn_last.get(op, 0) < _WARN_INTERVAL:
            return
        _warn_last[op] = now
    state.log.warning('存储层 %s 失败: %s: %s', op, type(err).__name__, err)


def _state_dir() -> Path:
    """当前生效的 state 目录：调用时取 state.STATE_DIR（含 monkeypatch）。"""
    return Path(state.STATE_DIR)


def _conn():
    d = _state_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = str(d / DB_NAME)
    with _LOCK:
        con = _DBS.get(path)
        if con is None:
            # isolation_level=None：语句级自动提交，需要原子性的地方显式 BEGIN IMMEDIATE（见 _tx）
            con = sqlite3.connect(path, check_same_thread=False, timeout=10, isolation_level=None)
            con.execute('PRAGMA journal_mode=WAL')
            con.execute('PRAGMA synchronous=NORMAL')
            con.executescript(_SCHEMA)
            con.execute('INSERT OR REPLACE INTO kv (k, v, ts) VALUES (?,?,?)',
                        ('schema_version', SCHEMA_VERSION, time.time()))
            _DBS[path] = con
        return con


def close_all():
    """关闭并忘掉全部连接（测试切换 STATE_DIR 后调用；生产进程不需要）。"""
    with _LOCK:
        for con in list(_DBS.values()):
            try:
                con.close()
            except sqlite3.Error:
                pass
        _DBS.clear()
        _warn_last.clear()


def _execute(sql, args=()):
    with _LOCK:
        return _conn().execute(sql, args)


@contextlib.contextmanager
def _tx():
    """读-改-写事务：进程内持 _LOCK，库级 BEGIN IMMEDIATE（防 CLI 等其它进程并发写）。"""
    with _LOCK:
        con = _conn()
        con.execute('BEGIN IMMEDIATE')
        try:
            yield con
        except BaseException:
            if con.in_transaction:
                con.execute('ROLLBACK')
            raise
        else:
            con.execute('COMMIT')


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False)


# ═══════════════════ 治理计划 ═══════════════════

def _upsert_plan(con, payload):
    pid = str(payload.get('id') or '')
    con.execute(
        'INSERT OR REPLACE INTO plans (id, ts, state, rule_sig, stats, executed_at, payload, updated_at)'
        ' VALUES (?,?,?,?,?,?,?,?)',
        (pid, float(payload.get('ts') or 0), str(payload.get('state') or ''),
         str(payload.get('rule_sig') or ''), _dumps(payload.get('stats') or {}),
         payload.get('executed_at'), _dumps(payload), time.time()))


def _plan_ok(payload):
    return isinstance(payload, dict) and payload.get('schema_version') == 2 and bool(payload.get('id'))


def db_save_plan(payload):
    """写入/更新一条计划（payload 为 save_plan 生成的完整 dict）。失败返回 False。"""
    if not _plan_ok(payload):
        return False
    try:
        with _LOCK:
            _upsert_plan(_conn(), payload)
        return True
    except _ERRORS as e:
        _warn('save_plan', e)
        return False


def db_load_plan(plan_id):
    """按 id 读计划（schema_v2 校验）；没有返回 None。"""
    try:
        row = _execute('SELECT payload FROM plans WHERE id=?', (str(plan_id),)).fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        return data if _plan_ok(data) else None
    except _ERRORS as e:
        _warn('load_plan', e)
        return None


def db_save_plan_state(plan_id, new_state, extra=None):
    """原子地更新计划状态（读库内 payload → 改 state/extra → 写回）。计划不存在返回 False。"""
    try:
        with _tx() as con:
            row = con.execute('SELECT payload FROM plans WHERE id=?', (str(plan_id),)).fetchone()
            if not row:
                return False
            data = json.loads(row[0])
            if not _plan_ok(data):
                return False
            data['state'] = new_state
            if extra:
                data.update(extra)
            _upsert_plan(con, data)
        return True
    except _ERRORS as e:
        _warn('save_plan_state', e)
        return False


def db_list_plans(limit=20):
    """最近计划列表（新→旧），元素与 /api/plans 既有结构一致。"""
    try:
        cur = _execute(
            'SELECT id, ts, state, stats, executed_at FROM plans ORDER BY ts DESC LIMIT ?',
            (int(limit),))
        out = []
        for pid, ts, st, stats, executed_at in cur.fetchall():
            try:
                stats_d = json.loads(stats) if stats else {}
            except ValueError:
                stats_d = {}
            out.append({'id': pid, 'ts': ts, 'state': st,
                        'stats': stats_d, 'executed_at': executed_at})
        return out
    except _ERRORS as e:
        _warn('list_plans', e)
        return []


def db_latest_pending_plan(ttl):
    """最近一份仍可执行（state=pending 且未超过 ttl 秒）的计划 id；没有返回 None。"""
    try:
        row = _execute('SELECT id FROM plans WHERE state=? AND ts>=? ORDER BY ts DESC LIMIT 1',
                       ('pending', time.time() - float(ttl))).fetchone()
        return row[0] if row else None
    except _ERRORS as e:
        _warn('latest_pending_plan', e)
        return None


def db_delete_plan(plan_id):
    try:
        _execute('DELETE FROM plans WHERE id=?', (str(plan_id),))
        return True
    except _ERRORS as e:
        _warn('delete_plan', e)
        return False


def db_purge_plans(cut_ts):
    """删除 ts 早于 cut_ts 的计划。返回删除条数。"""
    try:
        cur = _execute('DELETE FROM plans WHERE ts < ?', (float(cut_ts),))
        return cur.rowcount or 0
    except _ERRORS as e:
        _warn('purge_plans', e)
        return 0


# ═══════════════════ 执行历史（审计） ═══════════════════

def _ts_epoch(ts):
    try:
        return datetime.strptime(str(ts), '%Y-%m-%d %H:%M:%S').timestamp()
    except ValueError:
        return time.time()


def _insert_audit(con, rec):
    con.execute(
        'INSERT INTO audit (ts, ts_epoch, category, title, details, rule_sig) VALUES (?,?,?,?,?,?)',
        (str(rec.get('ts') or ''), _ts_epoch(rec.get('ts')), str(rec.get('category') or ''),
         str(rec.get('title') or ''), _dumps([str(d) for d in (rec.get('details') or [])]),
         str(rec.get('rule_sig') or '')))


def db_add_audit(rec):
    """追加一条审计记录（rec 结构见 logger.write）。失败返回 False。"""
    try:
        with _LOCK:
            _insert_audit(_conn(), rec)
        return True
    except _ERRORS as e:
        _warn('add_audit', e)
        return False


def db_recent_audit(limit=50):
    """最近 N 条审计（新→旧，按记录时间排序，同一秒内按写入顺序）。"""
    try:
        cur = _execute(
            'SELECT ts, category, title, details, rule_sig FROM audit ORDER BY ts DESC, id DESC LIMIT ?',
            (int(limit),))
        out = []
        for ts, category, title, details, rule_sig in cur.fetchall():
            try:
                details_l = json.loads(details) if details else []
            except ValueError:
                details_l = []
            out.append({'schema_version': 2, 'ts': ts, 'category': category,
                        'title': title, 'details': details_l, 'rule_sig': rule_sig or ''})
        return out
    except _ERRORS as e:
        _warn('recent_audit', e)
        return []


def db_audit_count():
    try:
        return int(_execute('SELECT COUNT(*) FROM audit').fetchone()[0])
    except _ERRORS as e:
        _warn('audit_count', e)
        return 0


def _import_audit_file(path):
    """导入一个 JSONL 审计文件（逐行解析，坏行跳过，单事务）。返回导入条数。"""
    n = 0
    with open(path, 'r', encoding='utf-8', errors='ignore') as f, _tx() as con:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or not rec.get('ts'):
                continue
            _insert_audit(con, rec)
            n += 1
    return n


# ═══════════════════ 追更订阅状态 ═══════════════════

def db_load_sub_state(store=None):
    """整份读订阅状态：{sid: payload}。store 缺省为 state.SUB_STORE（沿用旧状态文件名作键）。"""
    try:
        cur = _execute('SELECT sid, payload FROM sub_state WHERE store=?', (str(store or state.SUB_STORE),))
        out = {}
        for sid, payload in cur.fetchall():
            try:
                out[sid] = json.loads(payload)
            except ValueError:
                continue
        return out
    except _ERRORS as e:
        _warn('load_sub_state', e)
        return {}


def db_save_sub_state(store, data):
    """整份替换订阅状态（DELETE+INSERT 一个事务）。"""
    try:
        store = str(store or state.SUB_STORE)
        rows = [(store, str(sid), _dumps(v), time.time())
                for sid, v in (data or {}).items() if isinstance(v, dict)]
        with _tx() as con:
            con.execute('DELETE FROM sub_state WHERE store=?', (store,))
            con.executemany(
                'INSERT OR REPLACE INTO sub_state (store, sid, payload, updated_at) VALUES (?,?,?,?)',
                rows)
        return True
    except _ERRORS as e:
        _warn('save_sub_state', e)
        return False


def db_clear_sub_state(store=None):
    """清空某份状态（= 旧版「删除状态文件即重置」）。"""
    try:
        _execute('DELETE FROM sub_state WHERE store=?', (str(store or state.SUB_STORE),))
        return True
    except _ERRORS as e:
        _warn('clear_sub_state', e)
        return False


# ═══════════════════ 订阅列表 ═══════════════════

def db_subs_list():
    """订阅列表（按保存顺序）。"""
    try:
        out = []
        for (payload,) in _execute('SELECT payload FROM subscriptions ORDER BY pos').fetchall():
            try:
                v = json.loads(payload)
            except ValueError:
                continue
            if isinstance(v, dict):
                out.append(v)
        return out
    except _ERRORS as e:
        _warn('subs_list', e)
        return []


def db_subs_replace(subs):
    """整份替换订阅列表（单事务），返回实际保存的列表；失败返回当前库内列表。"""
    clean = [s for s in (subs or []) if isinstance(s, dict)]
    try:
        now = time.time()
        rows, seen = [], set()
        for i, s in enumerate(clean):
            key = str(s.get('id') or s.get('tmdb_id') or s.get('name') or '') or '_%d' % i
            if key in seen:          # 主键冲突（重复订阅）不能让整次保存失败：加序号区分
                key = '%s#%d' % (key, i)
            seen.add(key)
            rows.append((key, i, _dumps(s), now))
        with _tx() as con:
            con.execute('DELETE FROM subscriptions')
            con.executemany('INSERT INTO subscriptions (id, pos, payload, updated_at) VALUES (?,?,?,?)', rows)
        return clean
    except _ERRORS as e:
        _warn('subs_replace', e)
        return db_subs_list()


# ═══════════════════ 文档（整份读写的状态/缓存） ═══════════════════

def db_doc_get(name, default=None):
    """读文档；不存在/损坏返回 default。"""
    try:
        row = _execute('SELECT payload FROM docs WHERE name=?', (str(name),)).fetchone()
        return json.loads(row[0]) if row else default
    except _ERRORS as e:
        _warn('doc_get:%s' % name, e)
        return default


def db_doc_meta(name):
    """{'version', 'updated_at'}；不存在返回 None。"""
    try:
        row = _execute('SELECT version, updated_at FROM docs WHERE name=?', (str(name),)).fetchone()
        return {'version': int(row[0]), 'updated_at': float(row[1])} if row else None
    except _ERRORS as e:
        _warn('doc_meta:%s' % name, e)
        return None


def _doc_write(con, name, payload, ver):
    """payload 为已编码的 JSON 文本。"""
    con.execute('INSERT OR REPLACE INTO docs (name, payload, version, updated_at) VALUES (?,?,?,?)',
                (name, payload, ver, time.time()))


def db_doc_put(name, obj):
    """整份写文档，返回新版本号（失败返回 0）。"""
    name = str(name)
    try:
        payload = _dumps(obj)
        with _tx() as con:
            row = con.execute('SELECT version FROM docs WHERE name=?', (name,)).fetchone()
            ver = (int(row[0]) if row else 0) + 1
            _doc_write(con, name, payload, ver)
        return ver
    except _ERRORS as e:
        _warn('doc_put:%s' % name, e)
        return 0


def db_doc_update(name, fn, default=None):
    """原子读-改-写：fn(当前值或 default 的副本) 返回新值（返回 None 表示就地修改了传入对象）。
    返回写入后的新值；库失败时返回 default 的副本（fn 自身抛出的异常照常上抛）。"""
    name = str(name)
    try:
        with _tx() as con:
            row = con.execute('SELECT payload, version FROM docs WHERE name=?', (name,)).fetchone()
            cur, ver = copy.deepcopy(default), 0
            if row:
                ver = int(row[1])
                try:
                    cur = json.loads(row[0])
                except ValueError as e:
                    _warn('doc_decode:%s' % name, e)   # 损坏的旧值按默认值重建，不能永久卡死更新
            new = fn(cur)
            if new is None:
                new = cur
            _doc_write(con, name, _dumps(new), ver + 1)
        return new
    except _ERRORS as e:
        _warn('doc_update:%s' % name, e)
        return copy.deepcopy(default)


def db_doc_delete(name):
    try:
        _execute('DELETE FROM docs WHERE name=?', (str(name),))
        return True
    except _ERRORS as e:
        _warn('doc_delete:%s' % name, e)
        return False


# ═══════════════════ TMDB 响应缓存（每个请求一行） ═══════════════════

def db_tmdb_get(key, ttl):
    """命中且未超过 ttl 秒返回数据，否则 None。"""
    try:
        row = _execute('SELECT payload, ts FROM tmdb_cache WHERE k=?', (str(key),)).fetchone()
        if not row or time.time() - float(row[1]) >= float(ttl):
            return None
        return json.loads(row[0])
    except _ERRORS as e:
        _warn('tmdb_get', e)
        return None


def db_tmdb_put(key, data, ts=None):
    try:
        _execute('INSERT OR REPLACE INTO tmdb_cache (k, payload, ts) VALUES (?,?,?)',
                 (str(key), _dumps(data), float(ts if ts is not None else time.time())))
        return True
    except _ERRORS as e:
        _warn('tmdb_put', e)
        return False


def db_tmdb_prune(max_age=TMDB_MAX_AGE):
    """删除超过 max_age 秒的缓存行，返回删除条数。"""
    try:
        cur = _execute('DELETE FROM tmdb_cache WHERE ts < ?', (time.time() - float(max_age),))
        return cur.rowcount or 0
    except _ERRORS as e:
        _warn('tmdb_prune', e)
        return 0


# ═══════════════════ KV / 消息去重 ═══════════════════

def db_kv_get(key, default=None):
    try:
        row = _execute('SELECT v FROM kv WHERE k=?', (str(key),)).fetchone()
        return row[0] if row else default
    except _ERRORS as e:
        _warn('kv_get', e)
        return default


def db_kv_set(key, value):
    try:
        _execute('INSERT OR REPLACE INTO kv (k, v, ts) VALUES (?,?,?)',
                 (str(key), str(value), time.time()))
        return True
    except _ERRORS as e:
        _warn('kv_set', e)
        return False


def db_kv_incr(key):
    """原子自增（不存在/非整数视为 0），返回新值；失败返回 0。"""
    try:
        with _tx() as con:
            row = con.execute('SELECT v FROM kv WHERE k=?', (str(key),)).fetchone()
            try:
                n = int(row[0]) + 1 if row else 1
            except (TypeError, ValueError):
                n = 1
            con.execute('INSERT OR REPLACE INTO kv (k, v, ts) VALUES (?,?,?)', (str(key), str(n), time.time()))
        return n
    except _ERRORS as e:
        _warn('kv_incr', e)
        return 0


def db_dedup_add(key):
    """登记一个已处理键（如 tg_upd_<update_id>），顺带偶发清理过期键。"""
    try:
        now = time.time()
        _execute('INSERT OR REPLACE INTO dedup (k, ts) VALUES (?,?)', (str(key), now))
        # 约每 200 次插入清一次过期键（避免表无限增长；用秒数取模免去额外计数器）
        if int(now) % 200 == 0:
            _execute('DELETE FROM dedup WHERE ts < ?', (now - DEDUP_TTL,))
        return True
    except _ERRORS as e:
        _warn('dedup_add', e)
        return False


def db_dedup_seen(key):
    try:
        return _execute('SELECT 1 FROM dedup WHERE k=?', (str(key),)).fetchone() is not None
    except _ERRORS as e:
        _warn('dedup_seen', e)
        return False


# ═══════════════════ 一次性迁移（旧 JSON 文件 → 库） ═══════════════════

def _legacy_path(spec):
    base, name = spec
    sd = _state_dir()
    return (sd if base == 'state' else sd.parent) / name


def _retire(path, summary):
    """导入完成的旧文件改名为 *.migrated；改名失败只告警（kv 标记已防止重复导入）。"""
    try:
        path.rename(path.with_name(path.name + '.migrated'))
    except OSError as e:
        state.log.warning('旧状态文件改名失败（已导入，不影响运行）: %s: %s', path, e)
        summary['errors'].append('rename %s' % path.name)


def _read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def _migrate_one(flag, path, importer, summary, key):
    """单个旧文件迁移：有 kv 标记跳过；文件不在只打标记；导入失败不打标记（下次启动重试）。"""
    if db_kv_get('migrated:' + flag):
        return
    if path.exists():
        try:
            n = importer(path)
        except ValueError as e:
            # 内容本身坏了（JSON 解析失败/结构不对）：重试也不会成功，标记并改名，按空状态继续
            state.log.warning('旧状态文件内容无效，已跳过: %s: %s', path, e)
            summary['errors'].append('%s: %s' % (path.name, e))
            n = 0
        except _ERRORS as e:
            state.log.warning('迁移旧状态文件失败（下次启动重试）: %s: %s', path, e)
            summary['errors'].append('%s: %s' % (path.name, e))
            return
        summary[key] = summary.get(key, 0) + (n if isinstance(n, int) else 1)
        db_kv_set('migrated:' + flag, str(int(time.time())))
        _retire(path, summary)
    else:
        db_kv_set('migrated:' + flag, str(int(time.time())))


def _import_plan(path):
    data = _read_json(path)
    if not _plan_ok(data):
        raise ValueError('不是 schema_v2 计划')
    with _LOCK:
        _upsert_plan(_conn(), data)
    return 1


def _import_sub_state(path):
    data = _read_json(path)
    if not isinstance(data, dict):
        raise ValueError('订阅状态不是对象')
    if not db_save_sub_state(state.SUB_STORE, data):
        raise sqlite3.OperationalError('写入 sub_state 失败')
    return len(data)


def _import_tmdb(path):
    data = _read_json(path)
    if not isinstance(data, dict):
        raise ValueError('TMDB 缓存不是对象')
    n = 0
    with _tx() as con:
        for k, v in data.items():
            if isinstance(v, dict) and 'data' in v:
                con.execute('INSERT OR REPLACE INTO tmdb_cache (k, payload, ts) VALUES (?,?,?)',
                            (str(k), _dumps(v['data']), float(v.get('ts') or 0)))
                n += 1
    return n


def _doc_importer(name):
    def imp(path):
        if not db_doc_put(name, _read_json(path)):
            raise sqlite3.OperationalError('写入文档 %s 失败' % name)
        return 1
    return imp


def _migrate_plans(summary):
    """计划文件逐个迁移（无全局标记：计划文件随时可能残留，逐个导入后改名）。坏文件改名跳过。"""
    for f in sorted(_state_dir().glob('plan_*.json')):
        try:
            summary['plans'] += _import_plan(f)
        except _ERRORS as e:
            state.log.warning('迁移计划文件失败（已跳过）: %s: %s', f, e)
            summary['errors'].append('%s: %s' % (f.name, e))
        _retire(f, summary)


def _migrate_audit(summary):
    """audit.jsonl（含轮转的 audit.*.jsonl）→ audit 表。
    兼容旧标记 audit_jsonl_imported（值为导入时刻）：旧版只导入过当时的 audit.jsonl，
    之后的记录都双写进了库——所以有该标记时，只补导入「标记之前就已轮转出去」的备份文件。"""
    if db_kv_get('migrated:audit'):
        return
    d = _legacy_path(LEGACY_AUDIT_DIR)
    cur = d / 'audit.jsonl'
    rotated = sorted(d.glob('audit.*.jsonl')) if d.is_dir() else []
    old_flag = db_kv_get('audit_jsonl_imported')
    try:
        flag_ts = float(old_flag) if old_flag else None
    except ValueError:
        flag_ts = 0.0
    files = []
    for f in rotated:
        if flag_ts is None or f.stat().st_mtime < flag_ts:
            files.append(f)
    if flag_ts is None and cur.exists():
        files.append(cur)
    try:
        for f in files:
            summary['audit'] += _import_audit_file(f)
    except _ERRORS as e:
        state.log.warning('迁移审计文件失败（下次启动重试）: %s', e)
        summary['errors'].append('audit: %s' % e)
        return
    db_kv_set('audit_jsonl_imported', str(int(time.time())))
    db_kv_set('migrated:audit', str(int(time.time())))
    for f in rotated + ([cur] if cur.exists() else []):
        _retire(f, summary)


def _migrate_config_subscriptions(summary):
    """config.json 里的旧 subscriptions 键 → subscriptions 表（表为空才导入），成功后从配置里删掉该键。"""
    raw = _cfg.load_config().get('subscriptions')
    if raw is None:
        return
    try:
        subs = json.loads(raw or '[]')
    except ValueError:
        subs = []
    clean = [s for s in subs if isinstance(s, dict)] if isinstance(subs, list) else []
    if clean and not db_subs_list():
        # db_subs_replace 失败时返回库内现状（此处为空）→ 不删配置键，下次启动重试
        if not db_subs_replace(clean):
            raise sqlite3.OperationalError('订阅列表写入失败')
        summary['subscriptions'] = len(clean)
    _cfg.update_config(lambda c: c.pop('subscriptions', None))


def db_migrate():
    """启动时调用：旧版 JSON 状态文件一次性导入库并改名 *.migrated（逐文件 kv 标记，幂等）。
    任何失败只记日志、不抛出。返回摘要 dict（errors 为失败明细）。"""
    summary = {'plans': 0, 'sub_state': 0, 'audit': 0, 'tmdb': 0, 'docs': 0, 'subscriptions': 0, 'errors': []}
    steps = [lambda: _migrate_plans(summary),
             lambda: _migrate_config_subscriptions(summary),
             lambda: _migrate_one('sub_state', _legacy_path(LEGACY_SUB_STATE_FILE),
                                  _import_sub_state, summary, 'sub_state'),
             lambda: _migrate_audit(summary),
             lambda: _migrate_one('tmdb_cache', _legacy_path(LEGACY_TMDB_FILE), _import_tmdb, summary, 'tmdb')]
    for name, spec in LEGACY_DOC_FILES.items():
        steps.append(lambda name=name, spec=spec: _migrate_one(
            'doc:' + name, _legacy_path(spec), _doc_importer(name), summary, 'docs'))
    for step in steps:
        try:
            step()
        except Exception as e:     # 迁移绝不阻断启动：兜住一切（含意外编程错误），记日志继续
            state.log.warning('存储迁移步骤异常: %s: %s', type(e).__name__, e)
            summary['errors'].append(str(e))
    if summary['tmdb']:
        db_tmdb_prune()
    return summary
