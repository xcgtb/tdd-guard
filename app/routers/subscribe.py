# -*- coding: utf-8 -*-
"""追更订阅 / 每日晨报（v1.7.7 自 main.py 拆出）"""
import time
from fastapi import APIRouter, Depends, HTTPException

from app import config as _cfg
from app import state, morning, subscribe
from app.routers.deps import auth

router = APIRouter()


@router.get('/api/subscriptions', dependencies=[Depends(auth)])
def api_get_subs():
    subs = subscribe.get_subscriptions()
    state = subscribe.load_sub_state()
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
    cfg = _cfg.load_config()
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
    subscribe.set_subscriptions(clean)
    try:
        subscribe.check_subscriptions(send_notify=False)
    except Exception:
        pass
    return {'status': 'success', 'subscriptions': subscribe.get_subscriptions()}


@router.post('/api/subscriptions/settings', dependencies=[Depends(auth)])
def api_subs_settings(body: dict = None):
    body = body or {}

    def _apply(cfg):
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
    cfg = _cfg.update_config(_apply)
    state.reload_config()
    return {'status': 'success',
            'enabled': cfg['subscribe_enabled'] == '1',
            'interval_min': int(cfg['subscribe_interval_min']),
            'check_tmdb': cfg['subscribe_check_tmdb'] == '1'}


@router.post('/api/subscriptions/check', dependencies=[Depends(auth)])
def api_subs_check_now():
    try:
        # 手动「立即检查」同样推送 Telegram；推送成功才记账，之后定时检查不会重复推同一批集
        r = subscribe.check_subscriptions(send_notify=True)
    except Exception as e:
        return {'status': 'error', 'message': str(e)}
    return {'status': 'success', **r}


@router.get('/api/morning', dependencies=[Depends(auth)])
def api_get_morning():
    return {'status': 'success', 'morning': _cfg.get_morning_report(), 'tz': state.tz_info()}


@router.post('/api/morning', dependencies=[Depends(auth)])
def api_set_morning(body: dict = None):
    body = body or {}
    kwargs = {}
    if 'enabled' in body: kwargs['enabled'] = bool(body['enabled'])
    if 'hour' in body:    kwargs['hour'] = int(body['hour'])
    if 'minute' in body:  kwargs['minute'] = int(body['minute'])
    if 'items' in body:   kwargs['items'] = body['items']
    if 'prescan_min' in body: kwargs['prescan_min'] = int(body['prescan_min'])
    result = _cfg.update_morning_report(**kwargs)
    return {'status': 'success', 'morning': result}


@router.post('/api/morning/preview', dependencies=[Depends(auth)])
def api_preview_morning(body: dict = None):
    body = body or {}
    mr = _cfg.get_morning_report()
    items = body.get('items') or mr.get('items') or []
    force = bool(body.get('force', False))
    try:
        # 缓存预览严格只读已有缓存；现场扫严格重建，二者不再互相兜底。
        text = morning.build_morning_report(items, force_refresh=force, cache_only=not force)
    except Exception as e:
        return {'status': 'error', 'message': str(e)}
    import re as _re, html as _html
    text = _html.unescape(_re.sub(r'</?(?:b|i|code|pre|blockquote)[^>]*>', '', text))  # 网页预览按纯文本显示，去掉 Telegram 标记
    return {'status': 'success', 'text': text, 'force': force}


@router.post('/api/morning/send', dependencies=[Depends(auth)])
def api_send_morning(body: dict = None):
    body = body or {}
    mr = _cfg.get_morning_report()
    items = body.get('items') or mr.get('items') or []
    # 立即发送与定时晨报保持同一口径：发送当前晨报缓存，不在 HTTP 请求里触发现场扫描。
    # 需要现场扫描请使用「预览（现场扫）」；API 调用方仍可显式传 force=true。
    force = bool(body.get('force', False))
    try:
        ok = morning.send_morning_report(items, force_refresh=force, mark_sent=False)
    except Exception as e:
        return {'status': 'error', 'message': str(e)}
    return {'status': 'success' if ok else 'error',
            'message': '✅ 已发送到 Telegram' if ok else '❌ 发送失败（检查 Telegram 配置）',
            'force': force}
