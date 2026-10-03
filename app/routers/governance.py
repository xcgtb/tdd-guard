# -*- coding: utf-8 -*-
"""双库治理：扫描 / 清理 / 计划 / 巡检 / 残留 / 手动完结（v1.7.7 自 main.py 拆出）

计划、手动完结标记等状态全部存 SQLite（app/storage.py），不再读写 state/ 下的 JSON 文件。
"""
import time, threading
from fastapi import APIRouter, Depends, HTTPException

from app import state, governance, wash, morning, storage, scheduler
from app.routers.deps import auth, Args, spawn

router = APIRouter()


@router.get('/api/plan/{plan_id}', dependencies=[Depends(auth)])
def api_get_plan(plan_id: str):
    # 查看单个 Plan 详情（白皮书 §17）
    data = governance.load_plan(plan_id)
    if data is None:
        raise HTTPException(404, 'Plan 不存在或格式过旧')
    return {'status': 'success', 'plan': data}


@router.get('/api/plans', dependencies=[Depends(auth)])
def api_list_plans(limit: int = 20):
    # 列出最近 Plan（白皮书 §17）：SQLite 查询
    if limit < 1: limit = 1
    if limit > 100: limit = 100
    plans = storage.db_list_plans(limit)
    return {'status': 'success', 'plans': plans}


@router.post('/api/check', dependencies=[Depends(auth)])
def api_check():
    t = spawn('inter_check', governance.action_inter_check, Args())
    return {'task_id': t.id, 'status': 'success'}


@router.post('/api/clean', dependencies=[Depends(auth)])
def api_clean(body: dict = None):
    body = body or {}
    plan_id = str(body.get('plan_id', '')).strip()
    dry = bool(body.get('dry_run', False))
    if not plan_id:
        raise HTTPException(400, '必须提供 plan_id（请先执行诊断）')
    t = spawn('inter_clean', governance.action_inter_clean, Args(plan=plan_id, dry_run=dry))
    return {'task_id': t.id, 'dry_run': dry, 'status': 'success'}


def _read_manual_done() -> dict:
    return morning.read_manual_done()


@router.get('/api/manual_done', dependencies=[Depends(auth)])
def api_get_manual_done():
    return {'status': 'success', 'items': _read_manual_done()}


@router.post('/api/manual_done', dependencies=[Depends(auth)])
def api_set_manual_done(body: dict = None):
    """标记 / 取消标记某部剧为「已完结」（body: id, name, done）。仅记录，不动任何文件。"""
    body = body or {}
    sid = str(body.get('id') or '').strip()
    if not sid:
        return {'status': 'error', 'message': '缺少剧集 id'}
    done = bool(body.get('done', True))
    name = str(body.get('name') or '')[:200]

    def _apply(data):
        data = data if isinstance(data, dict) else {}
        if done:
            data[sid] = {'name': name, 'ts': int(time.time())}
        else:
            data.pop(sid, None)
        return data
    # 原子读-改-写（替代原来的进程内锁 + 整文件重写）；库不可用时返回 None
    data = storage.db_doc_update(storage.DOC_MANUAL_DONE, _apply, None)
    if data is None:
        return {'status': 'error', 'message': '保存失败（数据库不可用）'}
    return {'status': 'success', 'items': data}


_orphan_lock = threading.Lock()


@router.get('/api/orphans', dependencies=[Depends(auth)])
def api_orphans(max_depth: int = 3):
    # 扫描未知/孤儿文件 + 无 strm 的孤儿目录（白皮书 §16，只报告不删）
    if not _orphan_lock.acquire(blocking=False):
        return {'status': 'busy', 'message': '已有孤儿扫描任务在跑，请稍候'}
    try:
        return wash.action_scan_orphans(Args(max_depth=max_depth))
    except Exception as e:
        return {'status': 'error', 'message': str(e)}
    finally:
        _orphan_lock.release()


@router.post('/api/orphans/clean', dependencies=[Depends(auth)])
def api_clean_orphan_dirs(body: dict = None):
    # 删除孤儿目录（前端传入 paths 列表；dry_run 默认 True 只预览）
    body = body or {}
    paths = body.get('paths') or []
    dry_run = bool(body.get('dry_run', True))
    return wash.clean_orphan_dirs(paths, dry_run=dry_run)


@router.post('/api/wash/empty-dirs/scan', dependencies=[Depends(auth)])
def api_wash_empty_scan(body: dict = None):
    """目录级残留扫描（照搬上游）：叶子目录内完全没有 .strm 的媒体目录。"""
    body = body or {}
    path = str(body.get('path') or '')
    limit = int(body.get('limit') or 100)
    if not path:
        raise HTTPException(400, '缺少扫描目录')
    if not _orphan_lock.acquire(blocking=False):
        raise HTTPException(409, '有扫描/清理正在进行，请稍后再试')
    try:
        return wash.scan_empty_dirs(path, limit=limit)
    finally:
        _orphan_lock.release()


@router.post('/api/wash/empty-dirs/clean', dependencies=[Depends(auth)])
def api_wash_empty_clean(body: dict = None):
    """执行空目录清理：先移入 residue_backup 备份目录（可恢复），并通知 Emby。"""
    body = body or {}
    paths = body.get('paths') or []
    if not paths:
        raise HTTPException(400, '缺少清理路径')
    if not _orphan_lock.acquire(blocking=False):
        raise HTTPException(409, '有扫描/清理正在进行，请稍后再试')
    try:
        return wash.clean_empty_dirs(paths)
    finally:
        _orphan_lock.release()


@router.get('/api/governance/latest', dependencies=[Depends(auth)])
def api_gov_latest():
    d = governance.load_latest_scan()
    if not d or not isinstance(d.get('result'), dict):
        return {'status': 'success', 'found': False}
    ts = float(d.get('ts') or 0)
    age = time.time() - ts
    ttl = state.PLAN_TTL
    pid = d.get('plan_id')
    usable, reason = True, ''
    if age > ttl:
        usable, reason = False, 'expired'
    elif pid:
        p = governance.load_plan(pid)
        st = (p or {}).get('state') if p else 'missing'
        if st == 'executing':
            usable, reason = False, 'running'
        elif st != 'pending':
            usable, reason = False, ('used' if st in ('done', 'failed') else 'expired')
    return {'status': 'success', 'found': True, 'usable': usable, 'reason': reason,
            'plan_id': pid, 'ts': ts, 'age_sec': int(age),
            'remaining_sec': max(0, int(ttl - age)), 'ttl_sec': int(ttl),
            'result': d['result'] if usable else None}


@router.get('/api/governance/summary', dependencies=[Depends(auth)])
def api_governance_summary():
    """双库治理单一事实入口：扫描事实 + 片库快照 + 入库事实 + 当前规则指纹。
    页面不再分别拼接多个接口后自行判断口径。"""
    try:
        latest = governance.load_latest_scan() or {}
        result = latest.get('result') if isinstance(latest.get('result'), dict) else {}
        consistency = morning.daily_consistency_snapshot(force_refresh=False)
        ts = float(latest.get('ts') or 0)
        age = max(0, time.time() - ts) if ts else None
        ttl = state.PLAN_TTL
        pid = latest.get('plan_id') or result.get('plan_id')
        plan = governance.load_plan(pid) if pid else None
        plan_state = (plan or {}).get('state') if plan else ('missing' if pid else 'none')
        current_rule_sig = str((consistency or {}).get('rule_sig') or '')
        scan_rule_sig = str(latest.get('rule_sig') or result.get('rule_sig') or '')
        rule_aligned = (not scan_rule_sig or not current_rule_sig or scan_rule_sig == current_rule_sig)
        if plan and plan.get('rule_sig') and current_rule_sig:
            rule_aligned = rule_aligned and plan.get('rule_sig') == current_rule_sig
        usable = bool(latest) and bool(ts) and age <= ttl and (plan_state in ('pending', 'none')) and rule_aligned
        return {'status':'success', 'schema_version':2,
                'scan': {'found': bool(latest), 'ts': ts, 'age_sec': int(age or 0),
                         'plan_id': pid or '', 'plan_state': plan_state, 'usable': usable,
                         'ttl_sec': int(ttl), 'remaining_sec': max(0, int(ttl-(age or 0))) if ts else 0,
                         'rule_sig': scan_rule_sig, 'current_rule_sig': current_rule_sig,
                         'rule_aligned': rule_aligned,
                         'result': result},
                'consistency': consistency}
    except Exception as e:
        return {'status':'error','message':str(e)}


@router.get('/api/governance/auto', dependencies=[Depends(auth)])
def api_get_gov_auto():
    return {'status': 'success', 'settings': scheduler.gov_auto_view()}


@router.post('/api/governance/auto', dependencies=[Depends(auth)])
def api_set_gov_auto(body: dict = None):
    return {'status': 'success', 'settings': scheduler.gov_auto_update(body or {})}
