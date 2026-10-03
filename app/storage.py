# -*- coding: utf-8 -*-
"""SQLite 存储层：治理计划 / 执行历史 / 追更订阅状态 / 消息去重。

设计约定（与全项目其它模块一致）：
- 库文件固定为 STATE_DIR/ttd-guard.db（WAL 模式）；路径随 engine.STATE_DIR 惰性解析，
  测试改 AGENT_DATA / monkeypatch 路径后自动落到新库，不串数据；
- 计划与订阅状态：JSON 文件仍是「文档/导出格式」，SQLite 是索引与兜底——
  写两边（先文件后库），读先文件后库（文件命中顺带回填库）；
- 执行历史（审计）：SQLite 为主读路径，JSONL 继续双写作为可 grep 的导出；
- 消息去重 / TG offset：只存库（此前完全没有持久化，重启会重复处理旧消息）；
- 所有 public 函数失败都静默降级返回默认值，绝不影响治理主流程。
"""
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

DB_NAME = 'ttd-guard.db'
PLAN_KEEP_DAYS = 7          # 计划保留 7 天（与 purge_old 一致）
DEDUP_TTL = 7 * 86400       # 去重键保留 7 天

_DBS = {}                   # path_str -> sqlite3.Connection（按路径缓存，测试换目录即换库）
_LOCK = threading.RLock()   # 可重入：_execute/db_save_sub_state 持锁时还会调 _conn()

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
"""


def _state_dir() -> Path:
    """当前生效的 state 目录。优先取 engine.STATE_DIR（含 monkeypatch），
    engine 不可用时（脚本单独 import 本模块）退回 AGENT_DATA 环境变量。"""
    try:
        from app import engine as _e
        return Path(_e.STATE_DIR)
    except ImportError:
        try:
            import engine as _e
            return Path(_e.STATE_DIR)
        except ImportError:
            pass
    return Path(os.environ.get('AGENT_DATA', '/data')) / 'state'


def _conn():
    d = _state_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = str(d / DB_NAME)
    with _LOCK:
        con = _DBS.get(path)
        if con is None:
            con = sqlite3.connect(path, check_same_thread=False, timeout=10)
            con.execute('PRAGMA journal_mode=WAL')
            con.execute('PRAGMA synchronous=NORMAL')
            con.executescript(_SCHEMA)
            _DBS[path] = con
        return con


def _execute(sql, args=()):
    with _LOCK:
        con = _conn()
        cur = con.execute(sql, args)
        con.commit()
        return cur


# ═══════════════════ 治理计划 ═══════════════════

def db_save_plan(payload):
    """写入/更新一条计划（payload 为 save_plan 生成的完整 dict）。失败返回 False。"""
    try:
        if not isinstance(payload, dict) or payload.get('schema_version') != 2:
            return False
        pid = str(payload.get('id') or '')
        if not pid:
            return False
        _execute(
            'INSERT OR REPLACE INTO plans (id, ts, state, rule_sig, stats, executed_at, payload, updated_at)'
            ' VALUES (?,?,?,?,?,?,?,?)',
            (pid, float(payload.get('ts') or 0), str(payload.get('state') or ''),
             str(payload.get('rule_sig') or ''), json.dumps(payload.get('stats') or {}, ensure_ascii=False),
             payload.get('executed_at'), json.dumps(payload, ensure_ascii=False), time.time()))
        return True
    except Exception:
        return False


def db_load_plan(plan_id):
    """按 id 读计划（schema_v2 校验与 JSON 文件路径一致）；没有返回 None。"""
    try:
        cur = _execute('SELECT payload FROM plans WHERE id=?', (str(plan_id),))
        row = cur.fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        if not isinstance(data, dict) or data.get('schema_version') != 2:
            return None
        return data
    except Exception:
        return None


def db_save_plan_state(plan_id, state, extra=None):
    """更新计划状态（先读库内 payload 再改，保持与文件版一致的合并语义）。"""
    try:
        data = db_load_plan(plan_id)
        if data is None:
            return False
        data['state'] = state
        if extra:
            data.update(extra)
        return db_save_plan(data)
    except Exception:
        return False


def db_list_plans(limit=20):
    """最近计划列表（新→旧），元素与 /api/plans 既有结构一致。"""
    try:
        cur = _execute(
            'SELECT id, ts, state, stats, executed_at FROM plans ORDER BY ts DESC LIMIT ?',
            (int(limit),))
        out = []
        for pid, ts, state, stats, executed_at in cur.fetchall():
            try:
                stats_d = json.loads(stats) if stats else {}
            except ValueError:
                stats_d = {}
            out.append({'id': pid, 'ts': ts, 'state': state,
                        'stats': stats_d, 'executed_at': executed_at})
        return out
    except Exception:
        return []


def db_delete_plan(plan_id):
    try:
        _execute('DELETE FROM plans WHERE id=?', (str(plan_id),))
        return True
    except Exception:
        return False


def db_purge_plans(cut_ts):
    """删除 ts 早于 cut_ts 的计划（与 JSON 文件的 7 天清理配套）。返回删除条数。"""
    try:
        cur = _execute('DELETE FROM plans WHERE ts < ?', (float(cut_ts),))
        return cur.rowcount or 0
    except Exception:
        return 0


def db_sync_plans_from_disk():
    """把 state 目录里的 plan_*.json 同步进索引（新增或文件比库新才 upsert）。
    /api/plans 列表前调一次，兼顾「测试/用户直接写文件」的场景。返回同步条数。"""
    n = 0
    try:
        d = _state_dir()
        for f in d.glob('plan_*.json'):
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            pid = f.stem.replace('plan_', '')
            try:
                row = _conn().execute('SELECT updated_at FROM plans WHERE id=?', (pid,)).fetchone()
            except Exception:
                row = None
            if row and float(row[0]) >= mtime:
                continue
            try:
                data = json.loads(f.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if db_save_plan(data):
                n += 1
    except Exception:
        pass
    return n


# ═══════════════════ 执行历史（审计） ═══════════════════

def db_add_audit(rec):
    """追加一条审计记录（rec 与 logger JSONL 行结构一致）。失败返回 False。"""
    try:
        _execute(
            'INSERT INTO audit (ts, ts_epoch, category, title, details, rule_sig) VALUES (?,?,?,?,?,?)',
            (str(rec.get('ts') or ''), time.time(), str(rec.get('category') or ''),
             str(rec.get('title') or ''), json.dumps(rec.get('details') or [], ensure_ascii=False),
             str(rec.get('rule_sig') or '')))
        return True
    except Exception:
        return False


def db_recent_audit(limit=50):
    """最近 N 条审计（新→旧），结构对齐 logger.read_recent 的返回。"""
    try:
        cur = _execute(
            'SELECT ts, category, title, details, rule_sig FROM audit ORDER BY id DESC LIMIT ?',
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
    except Exception:
        return []


def _import_audit_file(path):
    """一次性导入既有 audit.jsonl（逐行解析，坏行跳过）。返回导入条数。"""
    n = 0
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
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
                if db_add_audit(rec):
                    n += 1
    except OSError:
        pass
    return n


# ═══════════════════ 追更订阅状态 ═══════════════════

def db_load_sub_state(store):
    """整份读订阅状态：{sid: payload}。store 为状态文件名（测试隔离用）。"""
    try:
        cur = _execute('SELECT sid, payload FROM sub_state WHERE store=?', (str(store),))
        out = {}
        for sid, payload in cur.fetchall():
            try:
                out[sid] = json.loads(payload)
            except ValueError:
                continue
        return out
    except Exception:
        return {}


def db_save_sub_state(store, state):
    """整份替换订阅状态（DELETE+INSERT 一个事务，语义对齐旧的整文件重写）。"""
    try:
        store = str(store)
        rows = [(store, str(sid), json.dumps(v, ensure_ascii=False), time.time())
                for sid, v in (state or {}).items() if isinstance(v, dict)]
        with _LOCK:
            con = _conn()
            con.execute('DELETE FROM sub_state WHERE store=?', (store,))
            con.executemany(
                'INSERT OR REPLACE INTO sub_state (store, sid, payload, updated_at) VALUES (?,?,?,?)',
                rows)
            con.commit()
        return True
    except Exception:
        return False


def db_clear_sub_state(store):
    """清空某份状态（状态文件被删除时对齐「重置」语义，防止旧状态从库里复活）。"""
    try:
        _execute('DELETE FROM sub_state WHERE store=?', (str(store),))
        return True
    except Exception:
        return False


# ═══════════════════ KV / 消息去重 ═══════════════════

def db_kv_get(key, default=None):
    try:
        cur = _execute('SELECT v FROM kv WHERE k=?', (str(key),))
        row = cur.fetchone()
        return row[0] if row else default
    except Exception:
        return default


def db_kv_set(key, value):
    try:
        _execute('INSERT OR REPLACE INTO kv (k, v, ts) VALUES (?,?,?)',
                 (str(key), str(value), time.time()))
        return True
    except Exception:
        return False


def db_dedup_add(key):
    """登记一个已处理键（如 tg_upd_<update_id>），顺带偶发清理过期键。"""
    try:
        now = time.time()
        _execute('INSERT OR REPLACE INTO dedup (k, ts) VALUES (?,?)', (str(key), now))
        # 约每 200 次插入清一次过期键（避免表无限增长；用秒数取模免去额外计数器）
        if int(now) % 200 == 0:
            _execute('DELETE FROM dedup WHERE ts < ?', (now - DEDUP_TTL,))
        return True
    except Exception:
        return False


def db_dedup_seen(key):
    try:
        cur = _execute('SELECT 1 FROM dedup WHERE k=?', (str(key),))
        return cur.fetchone() is not None
    except Exception:
        return False


# ═══════════════════ 一次性迁移 ═══════════════════

def db_migrate():
    """启动时调用一次：计划 JSON → 库；订阅状态 JSON → 库；audit.jsonl → 库。
    幂等（计划按 updated_at 跳过、审计有 kv 标记），重复执行无副作用。返回摘要 dict。"""
    summary = {'plans': 0, 'sub_state': 0, 'audit': 0}
    try:
        summary['plans'] = db_sync_plans_from_disk()
    except Exception:
        pass
    try:
        d = _state_dir()
        sub_file = d / 'subscriptions_state.json'
        if sub_file.exists():
            data = json.loads(sub_file.read_text(encoding='utf-8'))
            if isinstance(data, dict) and data:
                if db_save_sub_state(sub_file.name, data):
                    summary['sub_state'] = len(data)
    except Exception:
        pass
    try:
        if not db_kv_get('audit_jsonl_imported'):
            audit_file = _state_dir().parent / 'records' / 'audit.jsonl'
            if audit_file.exists():
                summary['audit'] = _import_audit_file(audit_file)
            db_kv_set('audit_jsonl_imported', str(int(time.time())))
    except Exception:
        pass
    return summary
