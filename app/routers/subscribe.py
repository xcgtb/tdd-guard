# -*- coding: utf-8 -*-
"""追更订阅 / 每日晨报（v1.7.7 自 main.py 拆出）"""
import time
from fastapi import APIRouter, Depends, HTTPException

try:
    from app.routers.deps import auth, engine
    from app.config import (load_config, save_config,
                            get_subscriptions, set_subscriptions,
                            get_morning_report, update_morning_report)
except ImportError:
    from routers.deps import auth, engine
    from config import (load_config, save_config,
                        get_subscriptions, set_subscriptions,
                        get_morning_report, update_morning_report)

router = APIRouter()


@router.get('/api/subscriptions', dependencies=[Depends(auth)])
def api_get_subs():
    subs = get_subscriptions()
    state = engine._load_sub_state()
    out = []
    for s in subs:
        sid = s.get('id') or s.get('tmdb_id') or s.get('name')
        info = dict(s)
        info['latest_ep'] = (state.get(sid) or {}).get('latest_ep', '')
        info['updated_at'] = (state.get(sid) or {}).get('updated_at', '')
        info['tmdb_total'] = (state.get(sid) or {}).get('tmdb_total', 0)
        info['tmdb_declared'] = (state.get(sid) or {}).get('tmdb_declared', 0)
        info['tmdb_status'] = (state.get(sid) or {}).get('tmdb_status', '')
        out.append(info)
    cfg = load_config()
    return {'status': 'success', 'subscriptions': out,
            'enabled': cfg.get('subscribe_enabled', '1') == '1',
            'interval_min': int(cfg.get('subscribe_interval_min') or 30),
            'check_tmdb': cfg.get('subscribe_check_tmdb', '1') == '1'}


@router.post('/api/subscriptions', dependencies=[Depends(auth)])
def api_save_subs(body: dict = None):
    body = body or {}
    subs = body.get('subscriptions') or []
    if not isinstance(subs, list):
        raise HTTPException(400, 'subscriptions 必须是数组')
    clean = []
    for s in subs:
        if not isinstance(s, dict): continue
        name = str(s.get('name') or '').strip()
        if not name: continue
        tmdb_id = s.get('tmdb_id')
        clean.append({
            'id': str(s.get('id') or tmdb_id or name),
            'name': name,
            'tmdb_id': str(tmdb_id) if tmdb_id else '',
            'poster': str(s.get('poster') or ''),
            'enabled': bool(s.get('enabled', True)),
            'added_at': s.get('added_at') or time.strftime('%Y-%m-%d %H:%M:%S'),
        })
    set_subscriptions(clean)
    try:
        engine.check_subscriptions(send_notify=False)
    except Exception:
        pass
    return {'status': 'success', 'subscriptions': get_subscriptions()}


@router.post('/api/subscriptions/settings', dependencies=[Depends(auth)])
def api_subs_settings(body: dict = None):
    body = body or {}
    cfg = load_config()
    if 'enabled' in body:
        cfg['subscribe_enabled'] = '1' if body['enabled'] else '0'
    if 'interval_min' in body:
        try:
            v = max(5, min(1440, int(body['interval_min'])))
        except (ValueError, TypeError):
            v = 30
        cfg['subscribe_interval_min'] = str(v)
    if 'check_tmdb' in body:
        cfg['subscribe_check_tmdb'] = '1' if body['check_tmdb'] else '0'
    save_config(cfg)
    engine.reload_config()
    return {'status': 'success',
            'enabled': cfg['subscribe_enabled'] == '1',
            'interval_min': int(cfg['subscribe_interval_min']),
            'check_tmdb': cfg['subscribe_check_tmdb'] == '1'}


@router.post('/api/subscriptions/check', dependencies=[Depends(auth)])
def api_subs_check_now():
    try:
        # 手动「立即检查」同样推送 Telegram；推送成功才记账，之后定时检查不会重复推同一批集
        r = engine.check_subscriptions(send_notify=True)
    except Exception as e:
        return {'status': 'error', 'message': str(e)}
    return {'status': 'success', **r}


@router.get('/api/morning', dependencies=[Depends(auth)])
def api_get_morning():
    return {'status': 'success', 'morning': get_morning_report(), 'tz': engine.tz_info()}


@router.post('/api/morning', dependencies=[Depends(auth)])
def api_set_morning(body: dict = None):
    body = body or {}
    kwargs = {}
    if 'enabled' in body: kwargs['enabled'] = bool(body['enabled'])
    if 'hour' in body:    kwargs['hour'] = int(body['hour'])
    if 'minute' in body:  kwargs['minute'] = int(body['minute'])
    if 'items' in body:   kwargs['items'] = body['items']
    if 'prescan_min' in body: kwargs['prescan_min'] = int(body['prescan_min'])
    result = update_morning_report(**kwargs)
    return {'status': 'success', 'morning': result}


@router.post('/api/morning/preview', dependencies=[Depends(auth)])
def api_preview_morning(body: dict = None):
    body = body or {}
    mr = get_morning_report()
    items = body.get('items') or mr.get('items') or []
    force = bool(body.get('force', False))
    try:
        # force=true → 立即现场扫描入库
        text = engine.build_morning_report(items, force_refresh=force)
    except Exception as e:
        return {'status': 'error', 'message': str(e)}
    import re as _re, html as _html
    text = _html.unescape(_re.sub(r'</?(?:b|i|code|pre|blockquote)[^>]*>', '', text))  # 网页预览按纯文本显示，去掉 Telegram 标记
    return {'status': 'success', 'text': text, 'force': force}


@router.post('/api/morning/send', dependencies=[Depends(auth)])
def api_send_morning(body: dict = None):
    body = body or {}
    mr = get_morning_report()
    items = body.get('items') or mr.get('items') or []
    force = bool(body.get('force', True))     # 默认立即扫描
    try:
        ok = engine.send_morning_report(items, force_refresh=force)
    except Exception as e:
        return {'status': 'error', 'message': str(e)}
    return {'status': 'success' if ok else 'error',
            'message': '✅ 已发送到 Telegram' if ok else '❌ 发送失败（检查 Telegram 配置）',
            'force': force}
