# -*- coding: utf-8 -*-
"""执行记录存储：JSONL 格式 + 自动轮转 + 旧日志迁移"""
import os, re, sys, json, time, threading
from pathlib import Path
from datetime import datetime

# 数据目录固定为 /data；AGENT_DATA 仅供测试使用，不对用户开放
DATA_DIR = Path(os.environ.get('AGENT_DATA', '/data'))
RECORDS_DIR = DATA_DIR / 'records'
RECORDS_FILE = RECORDS_DIR / 'audit.jsonl'
LEGACY_LOG = DATA_DIR / '媒体治理明细.log'

MAX_SIZE = 5 * 1024 * 1024      # 5MB 自动轮转
KEEP_BACKUPS = 3                 # 保留 3 个历史备份
READ_TAIL_BYTES = 2 * 1024 * 1024  # 读取时最多读末尾 2MB
_LOCK = threading.Lock()


def _ensure_dir():
    RECORDS_DIR.mkdir(parents=True, exist_ok=True)


def write(category: str, title: str, details=None):
    """追加一条记录（纯追加，不重写整个文件）"""
    rec = {
        'ts': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'category': str(category),
        'title': str(title),
        'details': [str(d) for d in (details or [])],
    }
    line = json.dumps(rec, ensure_ascii=False) + '\n'
    with _LOCK:
        _ensure_dir()
        _rotate_if_needed()
        try:
            with open(RECORDS_FILE, 'a', encoding='utf-8') as f:
                f.write(line)
        except OSError as e:
            print(f'[logger] write failed: {e}', file=sys.stderr)


def _rotate_if_needed():
    if not RECORDS_FILE.exists():
        return
    try:
        if RECORDS_FILE.stat().st_size < MAX_SIZE:
            return
    except OSError:
        return
    stamp = time.strftime('%Y%m%d-%H%M%S')
    backup = RECORDS_DIR / f'audit.{stamp}.jsonl'
    try:
        RECORDS_FILE.rename(backup)
    except OSError:
        return
    # 清理过旧的备份
    backups = sorted(RECORDS_DIR.glob('audit.*.jsonl'), reverse=True)
    for old in backups[KEEP_BACKUPS:]:
        try:
            old.unlink()
        except OSError:
            pass


def read_recent(limit: int = 50) -> list:
    """从文件末尾往前读 N 条（不读整个文件）"""
    if not RECORDS_FILE.exists():
        return []
    try:
        size = RECORDS_FILE.stat().st_size
        chunk = min(size, READ_TAIL_BYTES)
        with open(RECORDS_FILE, 'rb') as f:
            f.seek(-chunk, 2)
            raw = f.read().decode('utf-8', errors='ignore')
        lines = [ln for ln in raw.split('\n') if ln.strip()]
        # 如果截断，第一条可能不完整，丢弃
        if size > chunk:
            lines = lines[1:]
        records = []
        for ln in lines[-limit:][::-1]:  # 倒序：最新在前
            try:
                records.append(json.loads(ln))
            except ValueError:
                continue
        return records
    except OSError:
        return []


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
    """把旧的 媒体治理明细.log 一次性迁移到 JSONL"""
    if not LEGACY_LOG.exists():
        return 0
    if RECORDS_FILE.exists() and RECORDS_FILE.stat().st_size > 0:
        return 0
    try:
        text = LEGACY_LOG.read_text(encoding='utf-8')
    except OSError:
        return 0
    _ensure_dir()
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
    if not records:
        return 0
    with open(RECORDS_FILE, 'w', encoding='utf-8') as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    # 备份旧文件
    try:
        LEGACY_LOG.rename(DATA_DIR / '媒体治理明细.log.migrated')
    except OSError:
        pass
    return len(records)