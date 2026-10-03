# -*- coding: utf-8 -*-
"""执行记录（审计）：只存 SQLite audit 表（旧版 records/audit.jsonl 由 storage.db_migrate 一次性导入）"""
import os, re
from pathlib import Path
from datetime import datetime

from . import storage as _storage

# 数据目录固定为 /data；AGENT_DATA 仅供测试使用，不对用户开放
DATA_DIR = Path(os.environ.get('AGENT_DATA', '/data'))
LEGACY_LOG = DATA_DIR / '媒体治理明细.log'


def write(category: str, title: str, details=None, rule_sig=None):
    """追加一条记录（落库失败由存储层记告警，不影响调用方）"""
    rec = {
        'schema_version': 2,
        'ts': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'category': str(category),
        'title': str(title),
        'details': [str(d) for d in (details or [])],
        'rule_sig': str(rule_sig or ''),
    }
    _storage.db_add_audit(rec)


def read_recent(limit: int = 50) -> list:
    """最近 N 条（新→旧）。"""
    return _storage.db_recent_audit(limit)


def to_text(limit: int = 50) -> str:
    """格式化为人类可读文本（备用，兼容旧接口）"""
    records = read_recent(limit)
    if not records:
        return ''
    out = []
    for r in records:
        out.append(f"[{r['ts']}] 【{r['category']}】 {r['title']}")
        for d in r.get('details', []):
            out.append(f'  {d}')
        out.append('')
    return '\n'.join(out)


def migrate_legacy():
    """把更早期的 媒体治理明细.log 一次性导入 audit 表（导入后改名 .migrated）"""
    if not LEGACY_LOG.exists():
        return 0
    # 与旧版一致：已经有执行记录（库里有 / 还没迁移的 audit.jsonl 非空）就不再导入更老的日志
    legacy_jsonl = DATA_DIR / 'records' / 'audit.jsonl'
    try:
        if _storage.db_audit_count() > 0 or (legacy_jsonl.exists() and legacy_jsonl.stat().st_size > 0):
            return 0
    except OSError:
        return 0
    try:
        text = LEGACY_LOG.read_text(encoding='utf-8')
    except OSError:
        return 0
    records = []
    for block in text.split('\n\n'):
        block = block.strip()
        if not block:
            continue
        lines = block.split('\n')
        head = lines[0].strip()
        m = re.match(r'^\[([\d\-: ]+)\]\s*【([^】]+)】\s*(.*)$', head)
        if not m:
            continue
        ts, cat, title = m.group(1), m.group(2), m.group(3).strip()
        details = [ln.strip() for ln in lines[1:] if ln.strip()]
        records.append({'ts': ts, 'category': cat, 'title': title, 'details': details})
    n = sum(1 for r in records if _storage.db_add_audit(r))
    if records and not n:
        return 0          # 库不可用：保留旧文件，下次启动重试
    # 备份旧文件
    try:
        LEGACY_LOG.rename(DATA_DIR / '媒体治理明细.log.migrated')
    except OSError:
        pass
    return n
