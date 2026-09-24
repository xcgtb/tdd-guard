# -*- coding: utf-8 -*-
"""Media Agent Web 层 —— FastAPI 后端"""
import os, sys, time, uuid, json, logging, threading, traceback, secrets
import urllib.parse, urllib.request
from pathlib import Path
from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import engine, bot, logger
from app import config as _cfg
from app.config import (
    load_config, save_config, EDITABLE_KEYS,
    SENSITIVE_KEYS, mask_config, mask_value, is_masked_value,
    get_strategy, update_strategy,
    get_subscriptions, set_subscriptions,
    get_morning_report, update_morning_report,
    get_ingest_cfg,
)


WEB_USER = (os.environ.get('WEB_USER', '').strip() or 'admin')
WEB_PASSWORD = os.environ.get('WEB_PASSWORD', '').strip()
ALLOW_NO_AUTH = os.environ.get('ALLOW_NO_AUTH', '').strip() == '1'

if not WEB_PASSWORD:
    _msg = (
        '未设置 WEB_PASSWORD：Web 管理台不做任何鉴权，'
        '任何能访问到这个端口的人都可以直接登录并执行清理。'
    )
    if ALLOW_NO_AUTH:
        logging.getLogger('media_agent').warning('%s 已通过 ALLOW_NO_AUTH=1 显式放行，继续启动。', _msg)
    else:
        logging.getLogger('media_agent').error(
            '%s 拒绝启动。请设置 WEB_USER / WEB_PASSWORD，'
            '或明确知道风险后设置 ALLOW_NO_AUTH=1 跳过此检查。', _msg
        )
        raise SystemExit(1)

app = FastAPI(title='TDD Guard', version='1.1')

TASKS = {}
TASK_LOCK = threading.Lock()
CURRENT = {'task': None}

bot.set_current_ref(CURRENT)
try:
    bot.start()
except Exception as _e:
    logging.getLogger('media_agent').warning('Bot 启动失败: %s', _e)

try:
    _n = logger.migrate_legacy()
    if _n:
        logging.getLogger('media_agent').info('迁移旧日志 %d 条到 JSONL', _n)
except Exception as _e:
    logging.getLogger('media_agent').warning('日志迁移失败: %s', _e)


_security = HTTPBasic(auto_error=False)


def auth(credentials: HTTPBasicCredentials = Depends(_security)):
    if not WEB_PASSWORD:
        return True  # 仅当 ALLOW_NO_AUTH=1 放行启动时才会走到这里
    ok = bool(credentials) and \
        secrets.compare_digest(credentials.username, WEB_USER) and \
        secrets.compare_digest(credentials.password, WEB_PASSWORD)
    if not ok:
        raise HTTPException(status_code=401, detail='用户名或密码错误',
                            headers={'WWW-Authenticate': 'Basic realm="TDD Guard"'})
    return True


# ═══════════════════ 任务系统 ═══════════════════
class _TaskStream:
    def __init__(self, task):
        self.task = task; self.buf = ''
    def write(self, s):
        self.buf += s
        while '\n' in self.buf:
            line, self.buf = self.buf.split('\n', 1)
            line = line.strip()
            if line:
                self.task.logs.append(line)
                if len(self.task.logs) > 500: del self.task.logs[:100]
    def flush(self):
        if self.buf.strip():
            self.task.logs.append(self.buf.strip()); self.buf = ''


class Task:
    def __init__(self, kind):
        self.id = uuid.uuid4().hex[:12]; self.kind = kind
        self.status = 'running'; self.result = None; self.error = None
        self.logs = []; self.ts = time.time(); self.stream = _TaskStream(self)
    def to_dict(self, with_logs=True):
        d = {'id': self.id, 'kind': self.kind, 'status': self.status,
             'result': self.result, 'error': self.error, 'ts': self.ts}
        if with_logs: d['logs'] = self.logs[-200:]
        return d


class Args:
    def __init__(self, kw='', plan='', dry_run=False, **kwargs):
        self.kw = kw; self.plan = plan; self.dry_run = dry_run
        for k, v in kwargs.items(): setattr(self, k, v)


def spawn(kind, fn, *fargs):
    with TASK_LOCK:
        cur = CURRENT['task']
        if cur is not None and cur.status == 'running':
            raise HTTPException(429, f'已有任务 [{cur.kind}] 正在执行，请稍候')
    t = Task(kind); TASKS[t.id] = t

    def worker():
        with TASK_LOCK: CURRENT['task'] = t
        old_err = sys.stderr; sys.stderr = t.stream
        handler = logging.StreamHandler(t.stream)
        handler.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
        engine.log.addHandler(handler)
        try:
            res = fn(*fargs)
            t.result = res
            t.status = 'error' if isinstance(res, dict) and res.get('status') == 'error' else 'success'
            if t.status == 'error': t.error = res.get('message', '未知错误')
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            t.error = f'{type(e).__name__}: {e}'; t.status = 'error'
        finally:
            engine.log.removeHandler(handler); sys.stderr = old_err
            with TASK_LOCK: CURRENT['task'] = None

    threading.Thread(target=worker, daemon=True).start()
    return t


STATIC_DIR = Path(__file__).parent.parent / 'static'


@app.get('/')
def index():
    idx = STATIC_DIR / 'index.html'
    if not idx.exists():
        return JSONResponse({'error': f'前端文件不存在: {idx}'}, status_code=500)
    return FileResponse(idx, headers={
        'Cache-Control': 'no-cache, no-store, must-revalidate',
        'Pragma': 'no-cache',
        'Expires': '0',
    })


if STATIC_DIR.exists():
    app.mount('/static', StaticFiles(directory=str(STATIC_DIR)), name='static')


# ═══════════════════ 后台轮询：入库 + 订阅 + 晨报 ═══════════════════
_bg_state = {
    'last_ingest_check': 0,
    'last_sub_check': 0,
    'last_morning_date': '',
    'last_morning_prescan_date': '',
}


def _bg_loop():
    """每 30 秒检查一次，决定是否触发：入库缓存刷新 / 订阅检查 / 晨报预扫 / 晨报发送"""
    time.sleep(15)      # 启动后缓 15 秒，等 Emby 就绪
    while True:
        try:
            cfg = load_config()
            now = time.time()

            # ── 入库监控（默认 5 分钟） ──
            try:
                ingest_enabled = cfg.get('ingest_enabled', '1') == '1'
                ingest_interval = max(60, int(cfg.get('ingest_interval_min') or 5) * 60)
            except (ValueError, TypeError):
                ingest_enabled = True; ingest_interval = 300
            if ingest_enabled and (now - _bg_state['last_ingest_check']) >= ingest_interval:
                _bg_state['last_ingest_check'] = now
                try:
                    engine.refresh_ingest_cache()
                except Exception as e:
                    engine.log.warning('入库缓存刷新失败: %s', e)

            # ── TMDB 对照（后台预热） ──
            try:
                scan_enabled = cfg.get('tmdb_scan_enabled', '1') == '1'
                scan_hours = int(cfg.get('tmdb_scan_interval_hours') or 24)
                last_ts = float(cfg.get('tmdb_scan_last_ts') or '0')
            except (ValueError, TypeError):
                scan_enabled = True; scan_hours = 24; last_ts = 0
            if scan_enabled and (now - last_ts) >= scan_hours * 3600:
                try:
                    engine.refresh_tmdb_scan()
                except Exception as e:
                    engine.log.warning('TMDB 对照失败: %s', e)

            # ── 订阅轮询 ──
            try:
                interval = max(300, int(cfg.get('subscribe_interval_min') or 30) * 60)
            except ValueError:
                interval = 1800
            if cfg.get('subscribe_enabled', '1') == '1' and (now - _bg_state['last_sub_check']) >= interval:
                _bg_state['last_sub_check'] = now
                try:
                    engine.check_subscriptions(send_notify=True)
                except Exception as e:
                    engine.log.warning('订阅检查失败: %s', e)

            # ── 晨报预扫 + 发送 ──
            try:
                mr = get_morning_report()
            except Exception:
                mr = {'enabled': False}

            if mr.get('enabled'):
                today = time.strftime('%Y-%m-%d')
                hour = int(mr.get('hour', 9)); minute = int(mr.get('minute', 0))
                prescan_min = int(mr.get('prescan_min', 5))
                lt = time.localtime()
                lt_minutes = lt.tm_hour * 60 + lt.tm_min
                target_minutes = hour * 60 + minute

                # ── 预扫：提前 N 分钟静默刷新入库缓存 ──
                if prescan_min > 0:
                    prescan_target = target_minutes - prescan_min
                    if prescan_target < 0: prescan_target += 24 * 60
                    in_prescan_window = (prescan_target <= lt_minutes < prescan_target + 5)
                    if in_prescan_window and mr.get('prescan_last_date') != today:
                        engine.log.info('晨报预扫触发')
                        try:
                            engine.refresh_ingest_cache()
                            _cfg.mark_morning_prescan(today)
                        except Exception as e:
                            engine.log.warning('晨报预扫失败: %s', e)

                # ── 到点发送 ──
                if lt_minutes >= target_minutes and mr.get('last_date') != today:
                    try:
                        ok = engine.send_morning_report(mr.get('items') or [], force_refresh=False)
                        if ok:
                            engine.log.info('晨报已发送')
                    except Exception as e:
                        engine.log.warning('晨报发送失败: %s', e)

        except Exception as e:
            engine.log.warning('后台轮询异常: %s', e)
        time.sleep(30)


threading.Thread(target=_bg_loop, daemon=True, name='bg-poller').start()


# ═══════════════════ 健康 / 仪表盘 ═══════════════════
@app.get('/api/health')
def health():
    return {'status': 'ok', 'time': time.strftime('%Y-%m-%d %H:%M:%S'),
            'paths': {
                'L_ROOT':       str(engine.L_ROOT) + (' ✅' if engine.L_ROOT.exists() else ' ❌'),
                'S_ROOT':       str(engine.S_ROOT) + (' ✅' if engine.S_ROOT.exists() else ' ❌'),
                'CLOUD_L_ROOT': str(engine.CLOUD_L_ROOT) + (' ✅' if engine.CLOUD_L_ROOT.exists() else ' ❌'),
                'DATA_DIR':     str(engine.DATA_DIR) + (' ✅' if engine.DATA_DIR.exists() else ' ❌'),
            },
            'current_task': CURRENT['task'].to_dict(with_logs=False) if CURRENT['task'] else None}


@app.get('/api/dashboard', dependencies=[Depends(auth)])
def dashboard():
    try:
        l_count = sum(1 for _ in engine.L_ROOT.rglob('*.strm')) if engine.L_ROOT.exists() else 0
        s_count = sum(1 for _ in engine.S_ROOT.rglob('*.strm')) if engine.S_ROOT.exists() else 0
        emby_ok = False
        try: emby_ok = bool(engine.emby_request('/System/Info', timeout=3))
        except Exception: pass
        tmdb_ok = bool(engine.RUNTIME_CFG.get('tmdb_key'))
        tg_ok = bool(engine.RUNTIME_CFG.get('telegram_bot_token')) and bool(engine.RUNTIME_CFG.get('telegram_chat_id'))
        subs = get_subscriptions()
        mr = get_morning_report()
        ing = get_ingest_cfg()

        ingest_cache = engine.read_ingest_cache()
        ingest_ts = ingest_cache.get('ts', 0) if ingest_cache else 0
        ingest_stats = (ingest_cache or {}).get('stats', {})

        last_scan = None
        try:
            plan_files = sorted(engine.STATE_DIR.glob('plan_*.json'),
                                key=lambda p: p.stat().st_mtime, reverse=True)
            if plan_files:
                data = json.loads(plan_files[0].read_text(encoding='utf-8'))
                last_scan = {'time': time.strftime('%m-%d %H:%M', time.localtime(data.get('ts', 0))),
                             'total': len(data.get('keys', []))}
        except Exception: pass
        bs = bot.status()
        return {'localCount': f'{l_count:,}', 'shareCount': f'{s_count:,}',
                'services': {
                    'emby': {'ok': emby_ok, 'host': engine.EMBY_HOST},
                    'tmdb': {'ok': tmdb_ok},
                    'telegram': {'ok': tg_ok, 'bot_running': bs.get('running'),
                                 'bot_username': bs.get('bot_username')}},
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
                'lastScan': last_scan}
    except Exception as e:
        return {'localCount': '0', 'shareCount': '0',
                'services': {'emby': {'ok': False, 'host': ''},
                             'tmdb': {'ok': False}, 'telegram': {'ok': False}},
                'lastScan': None, 'error': str(e)}


@app.get('/api/task/{tid}', dependencies=[Depends(auth)])
def get_task(tid: str):
    t = TASKS.get(tid)
    if not t: raise HTTPException(404, '任务不存在或已过期')
    return t.to_dict()


# ═══════════════════ 双库治理 ═══════════════════
@app.post('/api/check', dependencies=[Depends(auth)])
def api_check():
    t = spawn('inter_check', engine.ACTIONS['inter_check'], Args())
    return {'task_id': t.id, 'status': 'success'}


@app.post('/api/clean', dependencies=[Depends(auth)])
def api_clean(body: dict = None):
    body = body or {}
    plan_id = str(body.get('plan_id', '')).strip()
    dry = bool(body.get('dry_run', False))
    if not plan_id:
        raise HTTPException(400, '必须提供 plan_id（请先执行诊断）')
    t = spawn('inter_clean', engine.ACTIONS['inter_clean'], Args(plan=plan_id, dry_run=dry))
    return {'task_id': t.id, 'dry_run': dry, 'status': 'success'}


# ═══════════════════ 入库监控 ═══════════════════
@app.get('/api/ingest', dependencies=[Depends(auth)])
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


@app.get('/api/ingest/status', dependencies=[Depends(auth)])
def api_ingest_status():
    """只读缓存状态，不触发刷新"""
    data = engine.read_ingest_cache()
    if not data:
        return {'status': 'success', 'has_cache': False}
    return {'status': 'success', 'has_cache': True,
            'ts': data.get('ts', 0),
            'age_sec': int(time.time() - data.get('ts', 0)),
            'stats': data.get('stats', {})}


# ═══════════════════ Emby 统计 / 追更 / 搜片 / 日志 ═══════════════════
@app.get('/api/stats', dependencies=[Depends(auth)])
def api_stats(full: int = 0, force: int = 0):
    kw = ('full ' if full else '') + ('force ' if force else '')
    return engine.ACTIONS['stats'](Args(kw=kw.strip()))


@app.get('/api/played', dependencies=[Depends(auth)])
def api_played():
    return engine.ACTIONS['played'](Args())


@app.get('/api/search', dependencies=[Depends(auth)])
def api_search(q: str):
    if not q.strip(): raise HTTPException(400, '关键词不能为空')
    return engine.ACTIONS['search'](Args(kw=q))


@app.get('/api/records', dependencies=[Depends(auth)])
def api_records(n: int = 50):
    return {'status': 'success', 'records': logger.read_recent(limit=n)}

@app.get('/api/logs', dependencies=[Depends(auth)])
def api_logs(n: int = 35):
    return {'status': 'success', 'text': logger.to_text(n)}

# ⚠️ 这个端点刻意不加鉴权：Emby item_id 是 32 位随机 GUID，无法枚举；
# 若加鉴权会与反代层的 Basic Auth realm 冲突，导致浏览器弹框/未授权。
# 详见 README「关于海报端点鉴权的说明」。
@app.get('/api/emby/poster/{item_id}')
def api_emby_poster(item_id: str):
    if not engine.EMBY_KEY: raise HTTPException(500, '未配置 EMBY_KEY')
    url = f'{engine.EMBY_HOST}/Items/{item_id}/Images/Primary'
    req = urllib.request.Request(url, headers={'X-Emby-Token': engine.EMBY_KEY})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read(); ct = r.headers.get('Content-Type', 'image/jpeg')
        return Response(content=data, media_type=ct)
    except Exception as e:
        raise HTTPException(404, str(e))


@app.get('/api/explore', dependencies=[Depends(auth)])
def api_explore(region: str = 'all', year: str = '', sort: str = 'popularity',
                media: str = 'movie', page: int = 1, q: str = ''):
    try:
        return engine.ACTIONS['explore'](Args(region=region, year=year,
                                              sort=sort, media=media,
                                              page=page, q=q))
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


@app.get('/api/tmdb/progress', dependencies=[Depends(auth)])
def api_tmdb_progress():
    return {'status': 'success', 'progress': engine.get_tmdb_scan_progress()}


@app.get('/api/emby/library', dependencies=[Depends(auth)])
def api_emby_library(force: int = 0, with_tmdb: int = 1):
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




@app.get('/api/library_stats', dependencies=[Depends(auth)])
def api_library_stats():
    try:
        return engine.action_library_stats(Args())
    except Exception as e:
        return {'status': 'error', 'message': str(e)}

# ═══════════════════ 治理策略 ═══════════════════
@app.get('/api/strategy', dependencies=[Depends(auth)])
def api_get_strategy():
    return {'status': 'success', 'strategy': get_strategy()}


@app.post('/api/strategy', dependencies=[Depends(auth)])
def api_set_strategy(body: dict = None):
    body = body or {}
    kwargs = {}
    if 'decision' in body:
        d = str(body['decision'])
        if d not in ('quality_first', 'keep_local', 'keep_share', 'balanced'):
            raise HTTPException(400, 'decision 必须是 quality_first / keep_local / keep_share / balanced')
        kwargs['decision'] = d
    if 'multi_season_protect' in body:
        kwargs['multi_season_protect'] = bool(body['multi_season_protect'])
    if 'tie_keep_local' in body:
        kwargs['tie_keep_local'] = bool(body['tie_keep_local'])
    if 'exempt_keywords' in body:
        kwargs['exempt_keywords'] = body['exempt_keywords']
    if 'special_action' in body:
        sa = str(body['special_action'])
        if sa not in ('keep', 'ignore', 'delete'):
            raise HTTPException(400, 'special_action 必须是 keep / ignore / delete')
        kwargs['special_action'] = sa
    result = update_strategy(**kwargs)
    engine.reload_config()
    return {'status': 'success', 'strategy': result}


# ═══════════════════ 白名单 ═══════════════════
@app.get('/api/exempt/scan', dependencies=[Depends(auth)])
def api_scan_exempt():
    try:
        matches = engine.scan_exempt_matches()
    except Exception as e:
        return {'status': 'error', 'message': str(e)}
    return {'status': 'success', 'matches': matches, 'keywords': engine._exempt_keywords()}


# ═══════════════════ 追更订阅 ═══════════════════
@app.get('/api/subscriptions', dependencies=[Depends(auth)])
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
        info['tmdb_status'] = (state.get(sid) or {}).get('tmdb_status', '')
        out.append(info)
    cfg = load_config()
    return {'status': 'success', 'subscriptions': out,
            'enabled': cfg.get('subscribe_enabled', '1') == '1',
            'interval_min': int(cfg.get('subscribe_interval_min') or 30),
            'check_tmdb': cfg.get('subscribe_check_tmdb', '1') == '1'}


@app.post('/api/subscriptions', dependencies=[Depends(auth)])
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


@app.post('/api/subscriptions/settings', dependencies=[Depends(auth)])
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


@app.post('/api/subscriptions/check', dependencies=[Depends(auth)])
def api_subs_check_now():
    try:
        r = engine.check_subscriptions(send_notify=False)
    except Exception as e:
        return {'status': 'error', 'message': str(e)}
    return {'status': 'success', **r}


# ═══════════════════ 入库监控设置 ═══════════════════
@app.get('/api/ingest/settings', dependencies=[Depends(auth)])
def api_get_ingest_settings():
    return {'status': 'success', 'settings': get_ingest_cfg()}


@app.post('/api/ingest/settings', dependencies=[Depends(auth)])
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


# ═══════════════════ 晨报 ═══════════════════
@app.get('/api/morning', dependencies=[Depends(auth)])
def api_get_morning():
    return {'status': 'success', 'morning': get_morning_report()}


@app.post('/api/morning', dependencies=[Depends(auth)])
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


@app.post('/api/morning/preview', dependencies=[Depends(auth)])
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
    return {'status': 'success', 'text': text, 'force': force}


@app.post('/api/morning/send', dependencies=[Depends(auth)])
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


# ═══════════════════ 配置 ═══════════════════
@app.get('/api/config', dependencies=[Depends(auth)])
def api_get_config(reveal: int = 0):
    cfg = load_config()
    if reveal:
        return {'status': 'success', 'config': cfg, 'masked': False}
    return {'status': 'success', 'config': mask_config(cfg), 'masked': True,
            'sensitive_keys': SENSITIVE_KEYS}


@app.post('/api/config', dependencies=[Depends(auth)])
def api_set_config(body: dict = None):
    body = body or {}
    cfg = load_config()
    for k in EDITABLE_KEYS:
        if k not in body: continue
        new_val = str(body[k]).strip()
        if k in SENSITIVE_KEYS and is_masked_value(new_val):
            continue
        cfg[k] = new_val
    saved = save_config(cfg)
    engine.reload_config()
    try: bot.restart()
    except Exception as e: logging.getLogger('media_agent').warning('Bot 重启失败: %s', e)
    return {'status': 'success', 'config': mask_config(saved), 'masked': True}


@app.post('/api/config/test/emby', dependencies=[Depends(auth)])
def api_test_emby(body: dict = None):
    body = body or {}
    host = (body.get('emby_host') or engine.EMBY_HOST or '').rstrip('/')
    key = body.get('emby_key') or ''
    if not key or is_masked_value(key): key = engine.EMBY_KEY or ''
    if not host or not key:
        return {'status': 'error', 'message': '请填写 Emby 地址和 API Key'}
    try:
        url = f'{host}/System/Info'
        req = urllib.request.Request(url, headers={'X-Emby-Token': key})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode('utf-8'))
        return {'status': 'success', 'message': f"✅ 连接成功\n服务器: {data.get('ServerName', '?')}\n版本: {data.get('Version', '?')}"}
    except Exception as e:
        return {'status': 'error', 'message': f'❌ 连接失败: {e}'}


@app.post('/api/config/test/tmdb', dependencies=[Depends(auth)])
def api_test_tmdb(body: dict = None):
    body = body or {}
    key = body.get('tmdb_key') or ''
    if not key or is_masked_value(key): key = engine.RUNTIME_CFG.get('tmdb_key') or ''
    if not key: return {'status': 'error', 'message': '请填写 TMDB API Key'}
    try:
        params = {'language': 'zh-CN'}; headers = {}
        if key.startswith('eyJ'): headers['Authorization'] = f'Bearer {key}'
        else: params['api_key'] = key
        url = 'https://api.themoviedb.org/3/configuration?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode('utf-8'))
        img = (data.get('images') or {}).get('secure_base_url', '?')
        return {'status': 'success', 'message': f'✅ TMDB Key 有效\n图片 CDN: {img}'}
    except Exception as e:
        return {'status': 'error', 'message': f'❌ 验证失败: {e}'}


@app.post('/api/config/test/telegram', dependencies=[Depends(auth)])
def api_test_telegram(body: dict = None):
    body = body or {}
    token = (body.get('telegram_bot_token') or '').strip()
    chat_id = (body.get('telegram_chat_id') or '').strip()
    if not token or is_masked_value(token): token = engine.RUNTIME_CFG.get('telegram_bot_token') or ''
    if not chat_id: chat_id = engine.RUNTIME_CFG.get('telegram_chat_id') or ''
    if not token or not chat_id:
        return {'status': 'error', 'message': '请填写 Bot Token 和 Chat ID'}
    try:
        text = '✅ <b>TDD Guard</b> 测试消息\n如果你看到这条消息，说明 Telegram 通知已配置成功。'
        url = f'https://api.telegram.org/bot{token}/sendMessage'
        data = urllib.parse.urlencode({'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'}).encode()
        req = urllib.request.Request(url, data=data, method='POST')
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read().decode('utf-8'))
        if resp.get('ok'): return {'status': 'success', 'message': '✅ 已发送测试消息，请查看 Telegram'}
        return {'status': 'error', 'message': f"❌ 发送失败: {resp.get('description', '未知错误')}"}
    except Exception as e:
        return {'status': 'error', 'message': f'❌ 发送失败: {e}'}


@app.get('/api/bot/status', dependencies=[Depends(auth)])
def api_bot_status():
    return {'status': 'success', 'bot': bot.status()}


def _gc():
    while True:
        time.sleep(600)
        cut = time.time() - 3600
        for k in [k for k, v in TASKS.items() if v.ts < cut]:
            TASKS.pop(k, None)


threading.Thread(target=_gc, daemon=True).start()




@app.get('/api/emby/series/{series_id}/episodes', dependencies=[Depends(auth)])
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
            if '\u5206\u4eab\u5f71\u89c6\u5e93' in path:
                lib = 'share'
            elif '\u5f71\u89c6\u5a92\u4f53\u5e93' in path:
                lib = 'local'
            else:
                lib = 'other'
            episodes.append({
                'season': ep.get('ParentIndexNumber'),
                'episode': ep.get('IndexNumber'),
                'lib': lib,
                'path': path,
            })
        return {'status': 'success', 'episodes': episodes}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}



@app.post('/api/emby/series/{series_id}/delete', dependencies=[Depends(auth)])
def api_emby_series_delete(series_id: str, body: dict = None):
    """删除某剧的全部文件
    target: local | share
    dry_run: True -> 只返回数量，不真删
    """
    body = body or {}
    target = str(body.get('target', '')).lower()
    dry_run = bool(body.get('dry_run', True))

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
        items = data.get('Items') or []
        files = []
        for ep in items:
            conv = engine.emby_path_to_container(ep.get('Path') or '')
            if not conv:
                continue
            in_local = str(conv).startswith(str(engine.L_ROOT))
            in_share = str(conv).startswith(str(engine.S_ROOT))
            if target == 'local' and not in_local:
                continue
            if target == 'share' and not in_share:
                continue
            if conv.exists():
                files.append(conv)

        if not files:
            return {'status': 'success', 'count': 0, 'target': target,
                    'message': '该库无此剧文件'}

        if dry_run:
            return {
                'status': 'success', 'dry_run': True,
                'count': len(files), 'target': target,
            }

        # 真删
        if target == 'local':
            result = engine.safe_delete_files(files, engine.L_ROOT, engine.CLOUD_L_ROOT, dry_run=False)
        else:
            result = engine.safe_delete_files(files, engine.S_ROOT, None, dry_run=False)

        engine.write_audit_log(
            '单剧删除',
            '删除【%s】库 %d 个文件' % (target, len(files)),
            ['strm: %d' % result['strm_removed'],
             'cloud: %d' % result['cloud_removed']] + result.get('errors', [])[:5]
        )

        return {
            'status': 'success', 'dry_run': False, 'target': target,
            'count': len(files),
            'strm_removed': result['strm_removed'],
            'cloud_removed': result['cloud_removed'],
            'errors': result.get('errors', []),
        }
    except Exception as e:
        return {'status': 'error', 'message': str(e)}

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=8321)