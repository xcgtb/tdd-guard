# -*- coding: utf-8 -*-
"""片库：入库 / 统计 / Emby / 搜索 / 日志 / 删除 / 探索（v1.7.7 自 main.py 拆出）"""
import time, json, threading, urllib.parse, urllib.request
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

try:
    from app.routers.deps import auth, engine, logger, Args
    from app.config import load_config, save_config, get_ingest_cfg
except ImportError:
    from routers.deps import auth, engine, logger, Args
    from config import load_config, save_config, get_ingest_cfg

router = APIRouter()


@router.get('/api/ingest', dependencies=[Depends(auth)])
def api_ingest(full: int = 0, force: int = 0):
    """
    full=1 完整清单，force=1 强制立即刷新
    默认读缓存（10 分钟时效），过期自动刷新
    """
    try:
        kw = ('full ' if full else '') + ('force ' if force else '')
        return engine.action_stats(Args(kw=kw.strip()))
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


@router.get('/api/ingest/status', dependencies=[Depends(auth)])
def api_ingest_status():
    """只读缓存状态，不触发刷新"""
    data = engine.read_ingest_cache()
    if not data:
        return {'status': 'success', 'has_cache': False}
    return {'status': 'success', 'has_cache': True,
            'ts': data.get('ts', 0),
            'age_sec': int(time.time() - data.get('ts', 0)),
            'ok': data.get('ok', True),
            'warning': data.get('stale_error') or ('' if data.get('ok', True) else data.get('error', '')),
            'stats': data.get('stats', {})}


@router.get('/api/stats', dependencies=[Depends(auth)])
def api_stats(full: int = 0, force: int = 0):
    kw = ('full ' if full else '') + ('force ' if force else '')
    return engine.ACTIONS['stats'](Args(kw=kw.strip()))


@router.get('/api/played', dependencies=[Depends(auth)])
def api_played():
    return engine.ACTIONS['played'](Args())


@router.get('/api/search', dependencies=[Depends(auth)])
def api_search(q: str):
    if not q.strip(): raise HTTPException(400, '关键词不能为空')
    return engine.ACTIONS['search'](Args(kw=q))


@router.get('/api/records', dependencies=[Depends(auth)])
def api_records(n: int = 50):
    try:
        snap = engine.daily_consistency_snapshot(False)
        return {'status': 'success', 'records': logger.read_recent(limit=n),
                'consistency': {'rule_sig': snap.get('rule_sig'),
                                'library_ts': (snap.get('library') or {}).get('ts', 0),
                                'ingest_ts': (snap.get('ingest') or {}).get('ts', 0),
                                'governance_ts': (snap.get('governance') or {}).get('ts', 0)}}
    except Exception:
        return {'status': 'success', 'records': logger.read_recent(limit=n), 'consistency': {}}


@router.get('/api/logs', dependencies=[Depends(auth)])
def api_logs(n: int = 35):
    return {'status': 'success', 'text': logger.to_text(n)}


@router.get('/api/emby/poster/{item_id}')
def api_emby_poster(item_id: str):
    if not engine.EMBY_KEY: raise HTTPException(500, '未配置 EMBY_KEY')
    url = f'{engine.EMBY_HOST}/Items/{urllib.parse.quote(item_id, safe="")}/Images/Primary'
    req = urllib.request.Request(url, headers={'X-Emby-Token': engine.EMBY_KEY})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read(); ct = r.headers.get('Content-Type', 'image/jpeg')
        return Response(content=data, media_type=ct)
    except Exception as e:
        raise HTTPException(404, str(e))


@router.get('/api/explore', dependencies=[Depends(auth)])
def api_explore(region: str = 'all', year: str = '', sort: str = 'popularity',
                media: str = 'movie', page: int = 1, q: str = '', genre: str = ''):
    try:
        return engine.ACTIONS['explore'](Args(region=region, year=year,
                                              sort=sort, media=media,
                                              page=page, q=q, genre=genre))
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


@router.get('/api/tmdb/progress', dependencies=[Depends(auth)])
def api_tmdb_progress():
    return {'status': 'success', 'progress': engine.get_tmdb_scan_progress()}


@router.get('/api/emby/library', dependencies=[Depends(auth)])
def api_emby_library(force: int = 0, with_tmdb: int = 1, cache_only: int = 0):
    # 只读缓存模式（进入「片库映射」页时用）：有 24 小时内的 TMDB 对照缓存就秒回，
    # 没有就返回 nocache，绝不因为打开页面而触发对照
    if cache_only:
        cached = engine.read_emby_lib_cache(max_age=24 * 3600)
        if cached:
            cached['from_cache'] = True
            return cached
        return {'status': 'nocache'}
    # 快速模式：不查 TMDB
    if not with_tmdb:
        return engine.action_emby_library(Args(force=False, with_tmdb=False))
    # 读缓存模式
    if not force:
        cached = engine.read_emby_lib_cache(max_age=6 * 3600)
        if cached:
            cached['from_cache'] = True
            return cached
    # force=1：启动后台任务，立即返回
    prog = engine.get_tmdb_scan_progress()
    if prog.get('running'):
        return {'status': 'running', 'message': 'TMDB 对照已在后台运行', 'progress': prog}
    import threading as _th
    _th.Thread(target=engine.refresh_tmdb_scan, daemon=True).start()
    return {'status': 'started', 'message': 'TMDB 对照已在后台启动'}


@router.get('/api/library_stats', dependencies=[Depends(auth)])
def api_library_stats():
    try:
        return engine.action_library_stats(Args())
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


@router.get('/api/ingest/settings', dependencies=[Depends(auth)])
def api_get_ingest_settings():
    return {'status': 'success', 'settings': get_ingest_cfg()}


@router.post('/api/ingest/settings', dependencies=[Depends(auth)])
def api_set_ingest_settings(body: dict = None):
    body = body or {}
    cfg = load_config()
    if 'enabled' in body:
        cfg['ingest_enabled'] = '1' if body['enabled'] else '0'
    if 'interval_min' in body:
        try:
            v = max(1, min(1440, int(body['interval_min'])))
        except (ValueError, TypeError):
            v = 5
        cfg['ingest_interval_min'] = str(v)
    save_config(cfg)
    engine.reload_config()
    return {'status': 'success', 'settings': get_ingest_cfg()}


@router.get('/api/emby/series/{series_id}/episodes', dependencies=[Depends(auth)])
def api_emby_series_episodes(series_id: str):
    """返回某剧所有分集（含 Path），前端可自行分库统计"""
    try:
        data = engine.emby_request('/Items', {
            'ParentId': series_id,
            'Recursive': 'true',
            'IncludeItemTypes': 'Episode',
            'Fields': 'Path,ParentIndexNumber,IndexNumber',
            'Limit': 5000,
        }) or {}
        episodes = []
        for ep in data.get('Items', []):
            path = ep.get('Path', '') or ''
            lib = engine.emby_lib_of(path) or 'other'
            conv = engine.emby_path_to_container(path)
            exists = True if conv is None else conv.exists()   # None = 映射不上，无法核实
            episodes.append({
                'season': ep.get('ParentIndexNumber'),
                'episode': ep.get('IndexNumber'),
                'lib': lib,
                'path': path,
                'exists': exists,
            })
        return {'status': 'success', 'episodes': episodes}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


def _delete_title_files(files, target, title, media_type, series_id=None, tmdb_id=None):
    """单剧/单片删除的公共执行段：拿跨入口文件锁 → 删文件（与双库治理同一条 fail-safe 删除链：
    本地库=删 strm + 联动 CD2 源文件；分享库=只删 strm）→ 立即修补缓存（海报马上撤下）
    → 后台精准通知 Emby → 写审计。"""
    root = engine.L_ROOT if target == 'local' else engine.S_ROOT
    cloud = engine.CLOUD_L_ROOT if target == 'local' else None
    try:
        with engine.mutation_lock():
            result = engine.safe_delete_files(files, root, cloud, dry_run=False)
    except engine.MutationBusy as e:
        return {'status': 'busy', 'message': str(e)}

    # 实际已删掉的文件（fail-safe 可能因云端源文件找不到而保留 strm）→ Emby 侧路径；空目录一并通知
    gone = [f for f in files if not f.exists()]
    emby_paths = set()
    for f in gone:
        emby_paths.add(engine.container_to_emby_path(f, target))
        for d in (f.parent, f.parent.parent):
            if not d.exists():
                emby_paths.add(engine.container_to_emby_path(d, target))

    engine.invalidate_media_caches(keep_emby_lib=True)
    series_removed, entry = False, None
    try:
        if series_id is not None:
            info = engine.patch_emby_lib_cache_after_series_delete(series_id, target)
            series_removed = info['remaining'] == 0
            entry = info['entry']
            if series_removed and info['series_path']:
                emby_paths.add(info['series_path'])   # 整部剧都没了：连剧目录一起通知
        elif tmdb_id is not None:
            engine.patch_emby_lib_cache_after_movie_delete(tmdb_id, target)
    except Exception as e:
        engine.log.warning('删除后缓存修补失败: %s', e)
    engine.notify_emby_deleted(emby_paths)    # 后台线程，不阻塞返回

    target_cn = '本地库' if target == 'local' else '分享库'
    cloud_note = '未处理云端源文件' if target == 'share' else ('删除 %d 个云端源文件' % result['cloud_removed'])
    engine.write_audit_log(
        '单剧删除',
        '《%s》删除%s：%d 个 strm' % (title, target_cn, result['strm_removed']),
        [
            '类型：%s' % media_type,
            '目标：%s' % target_cn,
            'strm 删除：%d 个' % result['strm_removed'],
            '云端源文件：%s' % cloud_note,
        ] + (['错误：%s' % e for e in result.get('errors', [])[:3]])
    )
    return {
        'status': 'success', 'dry_run': False, 'target': target,
        'count': len(files),
        'strm_removed': result['strm_removed'],
        'cloud_removed': result['cloud_removed'],
        'errors': result.get('errors', []),
        'series_removed': series_removed, 'entry': entry,
    }


@router.post('/api/emby/series/{series_id}/delete', dependencies=[Depends(auth)])
def api_emby_series_delete(series_id: str, body: dict = None):
    """删除某剧的全部文件
    target: local | share
    dry_run: True -> 只返回数量，不真删
    """
    body = body or {}
    target = str(body.get('target', '')).lower()
    dry_run = bool(body.get('dry_run', True))
    series_name = str(body.get('series_name', '')).strip() or '(未命名)'
    media_type = str(body.get('media_type', '')).strip() or '剧集'

    if target not in ('local', 'share'):
        return {'status': 'error', 'message': 'target 必须是 local 或 share'}

    try:
        data = engine.emby_request('/Items', {
            'ParentId': series_id,
            'Recursive': 'true',
            'IncludeItemTypes': 'Episode',
            'Fields': 'Path',
            'Limit': 5000,
        }) or {}
        root = engine.L_ROOT if target == 'local' else engine.S_ROOT
        files = []
        for ep in data.get('Items') or []:
            conv = engine.emby_path_to_container(ep.get('Path') or '')
            if conv and engine._inside(conv, root) and conv.exists():
                files.append(conv)

        if not files:
            return {'status': 'success', 'count': 0, 'target': target,
                    'message': '该库无此剧文件', 'ghost': True}
        if dry_run:
            return {'status': 'success', 'dry_run': True, 'count': len(files), 'target': target}
        return _delete_title_files(files, target, series_name, media_type, series_id=series_id)
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


@router.post('/api/emby/series/{series_id}/resync', dependencies=[Depends(auth)])
def api_emby_series_resync(series_id: str):
    """不删任何文件：以磁盘真实文件为准，重算/移除片库映射缓存里的这部剧，并通知 Emby 刷新。
    用于清掉「文件已经没了但海报还在」的失效条目。"""
    try:
        info = engine.patch_emby_lib_cache_after_series_delete(series_id)
        engine.invalidate_media_caches(keep_emby_lib=True)
        if info['remaining'] == 0 and info['series_path']:
            engine.notify_emby_deleted([info['series_path']])
        return {'status': 'success', 'remaining': info['remaining'],
                'series_removed': info['remaining'] == 0, 'entry': info['entry']}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


@router.post('/api/emby/movie/delete_by_tmdb', dependencies=[Depends(auth)])
def api_movie_delete(body: dict = None):
    """按 tmdb_id 删除电影（扫目录名，不依赖 Emby 索引）"""
    body = body or {}
    tmdb_id = str(body.get('tmdb_id', '')).strip()
    target = str(body.get('target', '')).lower()
    dry_run = bool(body.get('dry_run', True))
    movie_name = str(body.get('name', '')).strip() or '(未命名)'

    if not tmdb_id.isdigit():
        return {'status': 'error', 'message': '缺少 tmdb_id'}
    if target not in ('local', 'share'):
        return {'status': 'error', 'message': 'target 必须是 local 或 share'}

    root = engine.L_ROOT if target == 'local' else engine.S_ROOT
    files = engine.find_movie_strms_by_tmdb(root, tmdb_id)
    if not files:
        return {'status': 'success', 'count': 0, 'target': target}
    if dry_run:
        return {'status': 'success', 'dry_run': True, 'count': len(files), 'target': target}
    return _delete_title_files(files, target, movie_name, '电影', tmdb_id=tmdb_id)
