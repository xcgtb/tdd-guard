# -*- coding: utf-8 -*-
"""系统：健康 / 任务查询 / 仪表盘 / 一致性 / 缓存 / Bot 状态（v1.7.7 自 main.py 拆出）"""
import time, threading
from fastapi import APIRouter, Depends, HTTPException

from app import config as _cfg
from app import state, governance, morning, ingest, emby, stats, bot, tasks, subscribe, sync
from app.routers.deps import auth, _current_task_dict, APP_VERSION

router = APIRouter()


@router.get('/api/health')
def health():
    return {'status': 'ok', 'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'tz': state.tz_info(),
            'paths': {
                'L_ROOT':       str(state.L_ROOT) + (' ✅' if state.L_ROOT.exists() else ' ❌'),
                'S_ROOT':       str(state.S_ROOT) + (' ✅' if state.S_ROOT.exists() else ' ❌'),
                'CLOUD_L_ROOT': str(state.CLOUD_L_ROOT) + (' ✅' if state.CLOUD_L_ROOT.exists() else ' ❌'),
                'DATA_DIR':     str(state.DATA_DIR) + (' ✅' if state.DATA_DIR.exists() else ' ❌'),
            },
            'current_task': _current_task_dict()}


@router.get('/api/consistency', dependencies=[Depends(auth)])
def api_consistency(force: int = 0):
    try:
        return {'status': 'success', 'snapshot': morning.daily_consistency_snapshot(force_refresh=bool(force))}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


@router.get('/api/library/health', dependencies=[Depends(auth)])
def library_health():
    """统一片库健康快照：只读缓存，过期后台刷新，页面请求不触发全量扫描。"""
    try:
        snap = morning.unified_health(max_age=1800)
        return {'status': 'success', **snap}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


@router.get('/api/dashboard', dependencies=[Depends(auth)])
def dashboard():
    try:
        l_count, s_count = stats._get_strm_counts()
        # Emby 探测缓存 30 秒
        now_ts = time.time()
        if not hasattr(dashboard, '_emby_cache'):
            dashboard._emby_cache = {'ts': 0, 'ok': False, 'host': ''}
        if now_ts - dashboard._emby_cache['ts'] > 30:
            try:
                emby_ok = bool(emby.emby_request('/System/Info', timeout=3, retries=0))
            except Exception:
                emby_ok = False
            dashboard._emby_cache = {'ts': now_ts, 'ok': emby_ok, 'host': state.EMBY_HOST}
        emby_ok = dashboard._emby_cache['ok']
        tmdb_ok = bool(state.RUNTIME_CFG.get('tmdb_key'))
        tg_ok = bool(state.RUNTIME_CFG.get('telegram_bot_token')) and bool(state.RUNTIME_CFG.get('telegram_chat_id'))
        subs = subscribe.get_subscriptions()
        mr = _cfg.get_morning_report()
        ing = _cfg.get_ingest_cfg()

        ingest_cache = ingest.read_ingest_cache()
        ingest_ts = ingest_cache.get('ts', 0) if ingest_cache else 0
        ingest_stats = (ingest_cache or {}).get('stats', {})

        # 治理总览的扫描时间必须来自统一的 gov_latest.json。
        # plan_*.json 只有在存在待处理项时才会生成，0 项扫描也必须能成为“最近一次扫描”。
        last_scan = None
        try:
            gov = governance.load_latest_scan() or {}
            gr = gov.get('result') or {}
            gts = float(gov.get('ts') or gr.get('scan_ts') or 0)
            if gts:
                last_scan = {
                    'time': time.strftime('%m-%d %H:%M', time.localtime(gts)),
                    'ts': gts, 'age_sec': max(0, int(time.time() - gts)),
                    'plan_id': gov.get('plan_id'),
                    'total': gr.get('total_clean_cnt', 0),
                    'local': gr.get('del_local_cnt', 0), 'share': gr.get('del_share_cnt', 0),
                    'local_files': gr.get('del_local_files', 0), 'share_files': gr.get('del_share_files', 0),
                    'protected': gr.get('protected_items', []).__len__() if isinstance(gr.get('protected_items'), list) else 0,
                    'exempted': gr.get('exempted_count', 0),
                    'quiet_skipped': gr.get('quiet_skipped', 0),
                    'reason_counts': gr.get('reason_counts') or {},
                }
        except Exception:
            pass
        health = morning.unified_health(max_age=1800)
        bs = bot.status()
        return {'version': APP_VERSION,
                'localCount': f'{l_count:,}', 'shareCount': f'{s_count:,}',
                'services': {
                    'emby': {'ok': emby_ok, 'host': state.EMBY_HOST},
                    'tmdb': {'ok': tmdb_ok},
                    'telegram': {'ok': tg_ok, 'bot_running': bs.get('running'),
                                 'bot_username': bs.get('bot_username'),
                                 'bot_state': bs.get('state')}},
                'subscriptions': {'total': len(subs),
                                  'enabled': sum(1 for s in subs if s.get('enabled', True))},
                'morningReport': {'enabled': mr.get('enabled'),
                                  'time': f"{mr.get('hour', 9):02d}:{mr.get('minute', 0):02d}",
                                  'last_date': mr.get('last_date', ''),
                                  'prescan_min': mr.get('prescan_min', 5)},
                'ingest': {'enabled': ing.get('enabled'),
                           'interval_min': ing.get('interval_min'),
                           'cache_ts': ingest_ts,
                           'cache_age_sec': int(time.time() - ingest_ts) if ingest_ts else None,
                           'movies': ingest_stats.get('movies', 0),
                           'series': ingest_stats.get('series', 0),
                           'episodes': ingest_stats.get('episodes', 0)},
                'libraryHealth': {
                    'ts': health.get('ts', 0),
                    'stats': health.get('stats') or {},
                    'episodes': health.get('episodes', 0),
                    'top_missing': health.get('top_missing') or [],
                    'age_sec': int(time.time() - (health.get('ts') or 0)) if health.get('ts') else None,
                },
                'lastScan': last_scan}
    except Exception as e:
        return {'localCount': '0', 'shareCount': '0',
                'services': {'emby': {'ok': False, 'host': ''},
                             'tmdb': {'ok': False}, 'telegram': {'ok': False}},
                'lastScan': None, 'error': str(e)}


@router.get('/api/task/{tid}', dependencies=[Depends(auth)])
def get_task(tid: str):
    t = tasks.manager.get(tid)
    if not t: raise HTTPException(404, '任务不存在或已过期')
    return t.to_dict()


@router.post('/api/cache/refresh', dependencies=[Depends(auth)])
def api_cache_refresh():
    """清除全部内存缓存并触发后台重建（STRM 计数 / 片库映射 / 统计 / 分集 / Emby 索引）。
    磁盘缓存文件保留作为兜底，后台重建完成后自动覆盖。"""
    try:
        stats.invalidate_media_caches()
        threading.Thread(target=morning._overview_bg_refresh, daemon=True,
                         name='cache-refresh-overview').start()
        threading.Thread(target=stats._strm_count_bg_refresh, daemon=True,
                         name='cache-refresh-strm').start()
        return {'status': 'success', 'message': '缓存已清除，后台正在重建'}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


@router.get('/api/bot/status', dependencies=[Depends(auth)])
def api_bot_status():
    return {'status': 'success', 'bot': bot.status()}


@router.get('/api/sync/versions', dependencies=[Depends(auth)])
def api_sync_versions():
    """多端同步：返回各数据域的当前版本号（GET，不进 CSRF 中间件）。
    顺带补跑「其它进程（如 CLI）写库」造成的缓存失效，保证本进程内存缓存与库一致。"""
    sync.check_external()
    return {'status': 'success', 'v': sync.versions(), 'ts': time.time()}
