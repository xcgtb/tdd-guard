# -*- coding: utf-8 -*-
"""Media Agent Web 层 —— FastAPI 后端"""
import os, sys, time, json, asyncio, logging, threading, secrets
import urllib.parse, urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import engine, bot, logger, scheduler, tasks
from app import security as _sec
from app import config as _cfg
from app.config import (
    load_config, save_config, EDITABLE_KEYS,
    SENSITIVE_KEYS, mask_config, mask_value, is_masked_value,
    get_strategy, update_strategy,
    get_cover_strategy, update_cover_strategy,
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

# 版本号由 Docker 构建时按 git tag 注入（APP_VERSION），本地直接运行显示 dev
APP_VERSION = os.environ.get('APP_VERSION', 'dev')


def _cd2_ready():
    """检查 CD2 是否就绪"""
    check_dirs = {'电影', '剧集', '儿童节目', '综艺', '动漫', '纪录片', '演唱会'}
    try:
        cd2 = engine.CLOUD_L_ROOT
        if not cd2.exists():
            return False
        dirs = {i.name for i in cd2.iterdir() if i.is_dir()}
        return bool(dirs & check_dirs)
    except OSError:
        return False


def _cd2_watchdog(max_wait=60, max_retries=5):
    """CD2 启动守门员：未就绪则退出容器让 Docker 重启"""
    import time as _t
    _logger = logging.getLogger('media_agent')
    counter_file = engine.DATA_DIR / '.cd2_retry_count'

    count = 0
    if counter_file.exists():
        try:
            parts = counter_file.read_text().strip().split(':')
            if len(parts) == 2 and _t.time() - float(parts[0]) < 300:
                count = int(parts[1])
        except (ValueError, OSError):
            count = 0

    start = _t.time()
    while _t.time() - start < max_wait:
        if _cd2_ready():
            _logger.info('CD2 挂载就绪（等 %.1f 秒，重试次数 %d）', _t.time() - start, count)
            try:
                counter_file.unlink(missing_ok=True)
            except OSError:
                pass
            return
        _t.sleep(2)

    count += 1
    if count >= max_retries:
        _logger.error('CD2 挂载 %d 秒内未就绪，已重试 %d 次，放弃等待', max_wait, count)
        try:
            counter_file.unlink(missing_ok=True)
        except OSError:
            pass
        return

    try:
        counter_file.write_text('%f:%d' % (_t.time(), count))
    except OSError:
        pass

    _logger.warning('CD2 挂载未就绪，第 %d/%d 次重试，退出容器让 Docker 重启', count, max_retries)
    _t.sleep(2)
    os._exit(42)


def _startup():
    """进程级副作用统一在这里启动（以前散落在模块导入时，测试/工具一 import 就起线程）"""
    _log = logging.getLogger('media_agent')
    try:
        _n = logger.migrate_legacy()
        if _n:
            _log.info('迁移旧日志 %d 条到 JSONL', _n)
    except Exception as e:
        _log.warning('日志迁移失败: %s', e)
    # CD2 启动守门员：仅在显式开启时运行
    if os.environ.get('ENABLE_CD2_WATCHDOG', '0').strip().lower() in ('1', 'true', 'yes', 'on'):
        threading.Thread(target=_cd2_watchdog, daemon=True, name='cd2-watchdog').start()
    try:
        bot.start()
    except Exception as e:
        _log.warning('Bot 启动失败: %s', e)
    scheduler.start()


def _shutdown():
    scheduler.stop()
    bot.stop()


@asynccontextmanager
async def lifespan(_app):
    _startup()
    try:
        yield
    finally:
        _shutdown()


app = FastAPI(title='TTD Guard', version=APP_VERSION, lifespan=lifespan)


_security = HTTPBasic(auto_error=False)

# ── Cookie 会话登录 ──
# 原来只有浏览器原生 Basic 弹框，手机 Safari 经常不记住，每次都要重输。
# 现在登录一次后写入 30 天有效的签名 Cookie；Basic 头依然兼容（脚本/curl 可继续用）。
SESSION_COOKIE = 'tdd_session'
SESSION_DAYS = int(os.environ.get('SESSION_DAYS', '30') or 30)
_SESSION_KEY = (os.environ.get('SESSION_SECRET', '').strip() or WEB_PASSWORD or 'noauth').encode()


def _sign_session(user: str, exp: int) -> str:
    return _sec.sign_session(_SESSION_KEY, user, exp)


def _verify_session(token: str) -> bool:
    return _sec.verify_session(_SESSION_KEY, WEB_USER, token)


# ── CSRF + 安全响应头 ──
# 反向代理改写了 Host 且没传 X-Forwarded-Host 时，用 ALLOWED_ORIGINS 补白名单（逗号分隔）。
_ALLOWED_HOSTS = _sec.parse_allowed_origins(os.environ.get('ALLOWED_ORIGINS', ''))


@app.middleware('http')
async def _security_mw(request: Request, call_next):
    if request.url.path.startswith('/api/') and _sec.csrf_blocked(
            request.method, request.headers, bool(request.cookies.get(SESSION_COOKIE)), _ALLOWED_HOSTS):
        return JSONResponse({'status': 'error', 'detail': '跨站请求被拒绝（Origin 校验失败）。'
                             '如通过反向代理访问，请设置 ALLOWED_ORIGINS。'}, status_code=403)
    resp = await call_next(request)
    for k, v in _sec.SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    return resp


def auth(request: Request = None, credentials: HTTPBasicCredentials = Depends(_security)):
    # 保持对旧式单元测试/内部调用的兼容：auth(credentials)。
    if isinstance(request, HTTPBasicCredentials) and not isinstance(credentials, HTTPBasicCredentials):
        credentials, request = request, None
    if not isinstance(credentials, HTTPBasicCredentials):
        credentials = None
    if not WEB_PASSWORD:
        return True  # 仅当 ALLOW_NO_AUTH=1 放行启动时才会走到这里
    tok = request.cookies.get(SESSION_COOKIE) if request is not None else None
    if tok and _verify_session(tok):
        return True
    ok = bool(credentials) and \
        secrets.compare_digest(credentials.username, WEB_USER) and \
        secrets.compare_digest(credentials.password, WEB_PASSWORD)
    if not ok:
        # 故意不返回 WWW-Authenticate：否则浏览器会弹原生登录框，前端改用自带登录页
        raise HTTPException(status_code=401, detail='未登录或登录已过期')
    return True


@app.post('/api/login')
async def api_login(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    u = str(body.get('username', '')); p = str(body.get('password', ''))
    if not WEB_PASSWORD or (secrets.compare_digest(u, WEB_USER) and secrets.compare_digest(p, WEB_PASSWORD)):
        exp = int(time.time()) + SESSION_DAYS * 86400
        resp = JSONResponse({'status': 'success'})
        secure = (request.headers.get('x-forwarded-proto', request.url.scheme) == 'https')
        resp.set_cookie(SESSION_COOKIE, _sign_session(WEB_USER, exp), max_age=SESSION_DAYS * 86400,
                        httponly=True, samesite='lax', secure=secure, path='/')
        return resp
    await asyncio.sleep(1)  # 简单减缓暴力猜测（async 端点里不能 time.sleep，会卡住整个事件循环）
    return JSONResponse({'status': 'error', 'detail': '用户名或密码错误'}, status_code=401)


@app.post('/api/logout')
def api_logout():
    resp = JSONResponse({'status': 'success'})
    resp.delete_cookie(SESSION_COOKIE, path='/')
    return resp


# ═══════════════════ 任务系统 ═══════════════════
# 任务登记与互斥在 app/tasks.py，Web / Bot / 定时巡检共用同一个 manager
class Args:
    def __init__(self, kw='', plan='', dry_run=False, **kwargs):
        self.kw = kw; self.plan = plan; self.dry_run = dry_run
        for k, v in kwargs.items(): setattr(self, k, v)


def spawn(kind, fn, *fargs):
    try:
        return tasks.manager.spawn(kind, fn, *fargs, source='web')
    except tasks.TaskBusy as e:
        raise HTTPException(429, str(e))


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


# ═══════════════════ 健康 / 仪表盘 ═══════════════════
@app.get('/api/health')
def health():
    return {'status': 'ok', 'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'tz': engine.tz_info(),
            'paths': {
                'L_ROOT':       str(engine.L_ROOT) + (' ✅' if engine.L_ROOT.exists() else ' ❌'),
                'S_ROOT':       str(engine.S_ROOT) + (' ✅' if engine.S_ROOT.exists() else ' ❌'),
                'CLOUD_L_ROOT': str(engine.CLOUD_L_ROOT) + (' ✅' if engine.CLOUD_L_ROOT.exists() else ' ❌'),
                'DATA_DIR':     str(engine.DATA_DIR) + (' ✅' if engine.DATA_DIR.exists() else ' ❌'),
            },
            'current_task': _current_task_dict()}


def _current_task_dict():
    cur = tasks.manager.running()
    return cur.to_dict(with_logs=False) if cur else None


@app.get('/api/consistency', dependencies=[Depends(auth)])
def api_consistency(force: int = 0):
    try:
        return {'status': 'success', 'snapshot': engine.daily_consistency_snapshot(force_refresh=bool(force))}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}

@app.get('/api/library/health', dependencies=[Depends(auth)])
def library_health():
    """统一片库健康快照：只读缓存，过期后台刷新，页面请求不触发全量扫描。"""
    try:
        snap = engine.unified_health(max_age=1800)
        return {'status': 'success', **snap}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


@app.get('/api/dashboard', dependencies=[Depends(auth)])
def dashboard():
    try:
        l_count, s_count = engine._get_strm_counts()
        # Emby 探测缓存 30 秒
        now_ts = time.time()
        if not hasattr(dashboard, '_emby_cache'):
            dashboard._emby_cache = {'ts': 0, 'ok': False, 'host': ''}
        if now_ts - dashboard._emby_cache['ts'] > 30:
            try:
                emby_ok = bool(engine.emby_request('/System/Info', timeout=3, retries=0))
            except Exception:
                emby_ok = False
            dashboard._emby_cache = {'ts': now_ts, 'ok': emby_ok, 'host': engine.EMBY_HOST}
        emby_ok = dashboard._emby_cache['ok']
        tmdb_ok = bool(engine.RUNTIME_CFG.get('tmdb_key'))
        tg_ok = bool(engine.RUNTIME_CFG.get('telegram_bot_token')) and bool(engine.RUNTIME_CFG.get('telegram_chat_id'))
        subs = get_subscriptions()
        mr = get_morning_report()
        ing = get_ingest_cfg()

        ingest_cache = engine.read_ingest_cache()
        ingest_ts = ingest_cache.get('ts', 0) if ingest_cache else 0
        ingest_stats = (ingest_cache or {}).get('stats', {})

        # 治理总览的扫描时间必须来自统一的 gov_latest.json。
        # plan_*.json 只有在存在待处理项时才会生成，0 项扫描也必须能成为“最近一次扫描”。
        last_scan = None
        try:
            gov = engine.load_latest_scan() or {}
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
        health = engine.unified_health(max_age=1800)
        bs = bot.status()
        return {'version': app.version,
                'localCount': f'{l_count:,}', 'shareCount': f'{s_count:,}',
                'services': {
                    'emby': {'ok': emby_ok, 'host': engine.EMBY_HOST},
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


@app.get('/api/task/{tid}', dependencies=[Depends(auth)])
def get_task(tid: str):
    t = tasks.manager.get(tid)
    if not t: raise HTTPException(404, '任务不存在或已过期')
    return t.to_dict()


@app.get('/api/plan/{plan_id}', dependencies=[Depends(auth)])
def api_get_plan(plan_id: str):
    # 查看单个 Plan 详情（白皮书 §17）
    data = engine.load_plan(plan_id)
    if data is None:
        raise HTTPException(404, 'Plan 不存在或格式过旧')
    return {'status': 'success', 'plan': data}


@app.get('/api/plans', dependencies=[Depends(auth)])
def api_list_plans(limit: int = 20):
    # 列出最近 Plan（白皮书 §17）
    if limit < 1: limit = 1
    if limit > 100: limit = 100
    plans = []
    files = sorted(engine.STATE_DIR.glob('plan_*.json'),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files:
        if len(plans) >= limit: break
        try:
            data = json.loads(f.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if data.get('schema_version') != 2:
            continue
        plans.append({
            'id': data.get('id'),
            'ts': data.get('ts'),
            'state': data.get('state'),
            'stats': data.get('stats', {}),
            'executed_at': data.get('executed_at'),
        })
    return {'status': 'success', 'plans': plans}


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
            'ok': data.get('ok', True),
            'warning': data.get('stale_error') or ('' if data.get('ok', True) else data.get('error', '')),
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
    try:
        snap = engine.daily_consistency_snapshot(False)
        return {'status': 'success', 'records': logger.read_recent(limit=n),
                'consistency': {'rule_sig': snap.get('rule_sig'),
                                'library_ts': (snap.get('library') or {}).get('ts', 0),
                                'ingest_ts': (snap.get('ingest') or {}).get('ts', 0),
                                'governance_ts': (snap.get('governance') or {}).get('ts', 0)}}
    except Exception:
        return {'status': 'success', 'records': logger.read_recent(limit=n), 'consistency': {}}

@app.get('/api/logs', dependencies=[Depends(auth)])
def api_logs(n: int = 35):
    return {'status': 'success', 'text': logger.to_text(n)}

# ⚠️ 这个端点刻意不加鉴权：Emby item_id 是 32 位随机 GUID，无法枚举；
# 若加鉴权会与反代层的 Basic Auth realm 冲突，导致浏览器弹框/未授权。
# 详见 README「关于海报端点鉴权的说明」。
@app.get('/api/emby/poster/{item_id}')
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


@app.get('/api/explore', dependencies=[Depends(auth)])
def api_explore(region: str = 'all', year: str = '', sort: str = 'popularity',
                media: str = 'movie', page: int = 1, q: str = '', genre: str = ''):
    try:
        return engine.ACTIONS['explore'](Args(region=region, year=year,
                                              sort=sort, media=media,
                                              page=page, q=q, genre=genre))
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


@app.get('/api/tmdb/progress', dependencies=[Depends(auth)])
def api_tmdb_progress():
    return {'status': 'success', 'progress': engine.get_tmdb_scan_progress()}


@app.get('/api/emby/library', dependencies=[Depends(auth)])
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




@app.get('/api/library_stats', dependencies=[Depends(auth)])
def api_library_stats():
    try:
        return engine.action_library_stats(Args())
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


# ═══════════════════ 手动完结（TMDB 季数 / 集数不准时人工标记） ═══════════════════
_manual_done_lock = threading.Lock()


def _manual_done_path():
    return engine.MANUAL_DONE_FILE


def _read_manual_done() -> dict:
    return engine.read_manual_done()


def _write_manual_done(data: dict):
    p = _manual_done_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    tmp.replace(p)


@app.get('/api/manual_done', dependencies=[Depends(auth)])
def api_get_manual_done():
    return {'status': 'success', 'items': _read_manual_done()}


@app.post('/api/manual_done', dependencies=[Depends(auth)])
def api_set_manual_done(body: dict = None):
    """标记 / 取消标记某部剧为「已完结」（body: id, name, done）。仅记录，不动任何文件。"""
    body = body or {}
    sid = str(body.get('id') or '').strip()
    if not sid:
        return {'status': 'error', 'message': '缺少剧集 id'}
    with _manual_done_lock:
        data = _read_manual_done()
        if body.get('done', True):
            data[sid] = {'name': str(body.get('name') or '')[:200], 'ts': int(time.time())}
        else:
            data.pop(sid, None)
        try:
            _write_manual_done(data)
        except OSError as e:
            return {'status': 'error', 'message': '保存失败：%s' % e}
    return {'status': 'success', 'items': data}


_orphan_lock = threading.Lock()


@app.get('/api/orphans', dependencies=[Depends(auth)])
def api_orphans(max_depth: int = 3):
    # 扫描未知/孤儿文件 + 无 strm 的孤儿目录（白皮书 §16，只报告不删）
    if not _orphan_lock.acquire(blocking=False):
        return {'status': 'busy', 'message': '已有孤儿扫描任务在跑，请稍候'}
    try:
        return engine.action_scan_orphans(Args(max_depth=max_depth))
    except Exception as e:
        return {'status': 'error', 'message': str(e)}
    finally:
        _orphan_lock.release()


@app.post('/api/orphans/clean', dependencies=[Depends(auth)])
def api_clean_orphan_dirs(body: dict = None):
    # 删除孤儿目录（前端传入 paths 列表；dry_run 默认 True 只预览）
    body = body or {}
    paths = body.get('paths') or []
    dry_run = bool(body.get('dry_run', True))
    return engine.clean_orphan_dirs(paths, dry_run=dry_run)


@app.post('/api/wash/empty-dirs/scan', dependencies=[Depends(auth)])
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
        return engine.scan_empty_dirs(path, limit=limit)
    finally:
        _orphan_lock.release()


@app.post('/api/wash/empty-dirs/clean', dependencies=[Depends(auth)])
def api_wash_empty_clean(body: dict = None):
    """执行空目录清理：先移入 residue_backup 备份目录（可恢复），并通知 Emby。"""
    body = body or {}
    paths = body.get('paths') or []
    if not paths:
        raise HTTPException(400, '缺少清理路径')
    if not _orphan_lock.acquire(blocking=False):
        raise HTTPException(409, '有扫描/清理正在进行，请稍后再试')
    try:
        return engine.clean_empty_dirs(paths)
    finally:
        _orphan_lock.release()


@app.post('/api/cache/refresh', dependencies=[Depends(auth)])
def api_cache_refresh():
    """清除全部内存缓存并触发后台重建（STRM 计数 / 片库映射 / 统计 / 分集 / Emby 索引）。
    磁盘缓存文件保留作为兜底，后台重建完成后自动覆盖。"""
    try:
        engine.invalidate_media_caches()
        threading.Thread(target=engine._overview_bg_refresh, daemon=True,
                         name='cache-refresh-overview').start()
        threading.Thread(target=engine._strm_count_bg_refresh, daemon=True,
                         name='cache-refresh-strm').start()
        return {'status': 'success', 'message': '缓存已清除，后台正在重建'}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}

# ═══════════════════ 治理策略 ═══════════════════
@app.get('/api/strategy', dependencies=[Depends(auth)])
def api_get_strategy():
    return {'status': 'success', 'strategy': get_strategy()}


@app.get('/api/cover-strategy', dependencies=[Depends(auth)])
def api_get_cover():
    """画质对比规则（8 维）。"""
    return {'status': 'success', 'strategy': get_cover_strategy()}


@app.post('/api/cover-strategy', dependencies=[Depends(auth)])
def api_set_cover(body: dict = None):
    body = body or {}
    try:
        result = update_cover_strategy(body.get('strategy') or {})
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {'status': 'success', 'strategy': result}


@app.post('/api/cover-compare', dependencies=[Depends(auth)])
def api_cover_compare(body: dict = None):
    """对比测试：给两个文件名，按当前规则逐维展示谁胜、为什么。a=分享版 b=本地版。"""
    body = body or {}
    a, b = str(body.get('a') or '').strip(), str(body.get('b') or '').strip()
    if not a or not b:
        raise HTTPException(400, '需要 a、b 两个文件名')
    return {'status': 'success', 'compare': engine._compare_meta(a, b)}


@app.post('/api/strategy', dependencies=[Depends(auth)])
def api_set_strategy(body: dict = None):
    body = body or {}
    kwargs = {}
    if 'decision' in body:
        d = str(body['decision'])
        if d == 'balanced':
            d = 'quality_first'
        if d not in ('quality_first', 'keep_local', 'keep_share'):
            raise HTTPException(400, 'decision 必须是 quality_first / keep_local / keep_share')
        kwargs['decision'] = d
    if 'match_strategy' in body:
        ms = str(body['match_strategy'])
        if ms not in ('title_year', 'tmdb_first'):
            raise HTTPException(400, 'match_strategy 必须是 title_year / tmdb_first')
        kwargs['match_strategy'] = ms
    if 'multi_season_protect' in body:
        mp = str(body['multi_season_protect'])
        if mp in ('1', 'true', 'on'):
            mp = 'full'
        elif mp in ('0', 'false', 'off'):
            mp = 'off'
        if mp not in ('off', 'compare', 'full'):
            raise HTTPException(400, 'multi_season_protect 必须是 off / compare / full')
        kwargs['multi_season_protect'] = mp
    if 'tie_keep_local' in body:
        kwargs['tie_keep_local'] = bool(body['tie_keep_local'])
    if 'season_replace_ratio' in body:
        try:
            ratio = float(body['season_replace_ratio'])
        except (TypeError, ValueError):
            raise HTTPException(400, 'season_replace_ratio 必须是 0.5~1 之间的数字')
        if not (0.5 <= ratio <= 1.0):
            raise HTTPException(400, 'season_replace_ratio 必须在 0.5 ~ 1 之间')
        kwargs['season_replace_ratio'] = ratio
    if 'exempt_keywords' in body:
        kwargs['exempt_keywords'] = body['exempt_keywords']
    if 'special_action' in body:
        sa = str(body['special_action'])
        if sa == 'keep':
            sa = 'ignore'
        if sa not in ('compare', 'ignore', 'delete'):
            raise HTTPException(400, 'special_action 必须是 compare / ignore / delete')
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


# ═══════════════════ 双库治理：最近一次扫描清单（含时效） ═══════════════════
@app.get('/api/governance/latest', dependencies=[Depends(auth)])
def api_gov_latest():
    d = engine.load_latest_scan()
    if not d or not isinstance(d.get('result'), dict):
        return {'status': 'success', 'found': False}
    ts = float(d.get('ts') or 0)
    age = time.time() - ts
    ttl = engine.PLAN_TTL
    pid = d.get('plan_id')
    usable, reason = True, ''
    if age > ttl:
        usable, reason = False, 'expired'
    elif pid:
        p = engine.load_plan(pid)
        st = (p or {}).get('state') if p else 'missing'
        if st == 'executing':
            usable, reason = False, 'running'
        elif st != 'pending':
            usable, reason = False, ('used' if st in ('done', 'failed') else 'expired')
    return {'status': 'success', 'found': True, 'usable': usable, 'reason': reason,
            'plan_id': pid, 'ts': ts, 'age_sec': int(age),
            'remaining_sec': max(0, int(ttl - age)), 'ttl_sec': int(ttl),
            'result': d['result'] if usable else None}


# ═══════════════════ 双库治理：定时巡检设置 ═══════════════════
@app.get('/api/governance/summary', dependencies=[Depends(auth)])
def api_governance_summary():
    """双库治理单一事实入口：扫描事实 + 片库快照 + 入库事实 + 当前规则指纹。
    页面不再分别拼接多个接口后自行判断口径。"""
    try:
        latest = engine.load_latest_scan() or {}
        result = latest.get('result') if isinstance(latest.get('result'), dict) else {}
        consistency = engine.daily_consistency_snapshot(force_refresh=False)
        ts = float(latest.get('ts') or 0)
        age = max(0, time.time() - ts) if ts else None
        ttl = engine.PLAN_TTL
        pid = latest.get('plan_id') or result.get('plan_id')
        plan = engine.load_plan(pid) if pid else None
        state = (plan or {}).get('state') if plan else ('missing' if pid else 'none')
        current_rule_sig = str((consistency or {}).get('rule_sig') or '')
        scan_rule_sig = str(latest.get('rule_sig') or result.get('rule_sig') or '')
        rule_aligned = (not scan_rule_sig or not current_rule_sig or scan_rule_sig == current_rule_sig)
        if plan and plan.get('rule_sig') and current_rule_sig:
            rule_aligned = rule_aligned and plan.get('rule_sig') == current_rule_sig
        usable = bool(latest) and bool(ts) and age <= ttl and (state in ('pending', 'none')) and rule_aligned
        return {'status':'success', 'schema_version':2,
                'scan': {'found': bool(latest), 'ts': ts, 'age_sec': int(age or 0),
                         'plan_id': pid or '', 'plan_state': state, 'usable': usable,
                         'ttl_sec': int(ttl), 'remaining_sec': max(0, int(ttl-(age or 0))) if ts else 0,
                         'rule_sig': scan_rule_sig, 'current_rule_sig': current_rule_sig,
                         'rule_aligned': rule_aligned,
                         'result': result},
                'consistency': consistency}
    except Exception as e:
        return {'status':'error','message':str(e)}

@app.get('/api/governance/auto', dependencies=[Depends(auth)])
def api_get_gov_auto():
    return {'status': 'success', 'settings': scheduler.gov_auto_view()}


@app.post('/api/governance/auto', dependencies=[Depends(auth)])
def api_set_gov_auto(body: dict = None):
    return {'status': 'success', 'settings': scheduler.gov_auto_update(body or {})}


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
    return {'status': 'success', 'morning': get_morning_report(), 'tz': engine.tz_info()}


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
    import re as _re, html as _html
    text = _html.unescape(_re.sub(r'</?(?:b|i|code|pre|blockquote)[^>]*>', '', text))  # 网页预览按纯文本显示，去掉 Telegram 标记
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
def _env_overridden_keys() -> list:
    # yml/.env 里显式设置了非空值的字段：Web 页存了也不会生效，前端应据此提示/禁用
    return [k for k, env_name in _cfg.ENV_OVERRIDE_KEYS.items() if os.environ.get(env_name)]


@app.get('/api/config', dependencies=[Depends(auth)])
def api_get_config(reveal: int = 0):
    cfg = load_config()
    env_overridden = _env_overridden_keys()
    if reveal:
        return {'status': 'success', 'config': cfg, 'masked': False, 'env_overridden': env_overridden}
    return {'status': 'success', 'config': mask_config(cfg), 'masked': True,
            'sensitive_keys': SENSITIVE_KEYS, 'env_overridden': env_overridden}


@app.post('/api/config', dependencies=[Depends(auth)])
def api_set_config(body: dict = None):
    body = body or {}
    cfg = load_config()
    env_overridden = set(_env_overridden_keys())
    skipped_env = []
    for k in EDITABLE_KEYS:
        if k not in body: continue
        if k in env_overridden:
            # yml 已经显式指定了这个字段，Web 页提交的值落盘也没用，直接跳过并告知前端
            skipped_env.append(k)
            continue
        new_val = str(body[k]).strip()
        if k in SENSITIVE_KEYS and is_masked_value(new_val):
            continue
        cfg[k] = new_val
    saved = save_config(cfg)
    engine.reload_config()
    try: bot.restart()
    except Exception as e: logging.getLogger('media_agent').warning('Bot 重启失败: %s', e)
    return {'status': 'success', 'config': mask_config(saved), 'masked': True,
            'env_overridden': list(env_overridden), 'skipped_env': skipped_env}


@app.post('/api/config/test/emby', dependencies=[Depends(auth)])
def api_test_emby(body: dict = None):
    body = body or {}
    raw_host = (body.get('emby_host') or '').strip()
    host_overridden = bool(raw_host) and raw_host.rstrip('/') != (engine.EMBY_HOST or '').rstrip('/')
    host = (raw_host or engine.EMBY_HOST or '').rstrip('/')
    key = body.get('emby_key') or ''
    if not key or is_masked_value(key):
        if host_overridden:
            # 换了地址就不能偷用旧地址保存的 Key 去测——否则真实 Key 会被发往调用方指定的任意 host
            return {'status': 'error', 'message': '更换地址后请填写完整的 API Key，不能沿用已保存的旧 Key'}
        key = engine.EMBY_KEY or ''
    if not host or not key:
        return {'status': 'error', 'message': '请填写 Emby 地址和 API Key'}
    try:
        url = f'{host}/System/Info'
        req = urllib.request.Request(url, headers={'X-Emby-Token': key})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode('utf-8'))
        msg = f"✅ 连接成功\n服务器: {data.get('ServerName', '?')}\n版本: {data.get('Version', '?')}"
        # 设置页未保存的路径也拿来比对，方便边改边测；没传就用当前生效的配置
        pm = engine.emby_path_map(body.get('emby_local_path', engine.EMBY_PATHS.local),
                                body.get('emby_share_path', engine.EMBY_PATHS.share))
        return {'status': 'success', 'message': msg + _emby_lib_paths_hint(host, key, pm)}
    except Exception as e:
        return {'status': 'error', 'message': f'❌ 连接失败: {e}'}


def _emby_lib_paths_hint(host, key, pm):
    """列出 Emby 媒体库的文件夹路径，并标出哪些对上了本地/分享库路径配置；
    拿不到就返回空串，绝不影响连接测试结果"""
    try:
        req = urllib.request.Request(f'{host}/Library/VirtualFolders', headers={'X-Emby-Token': key})
        with urllib.request.urlopen(req, timeout=5) as r:
            folders = json.loads(r.read().decode('utf-8'))
        names = {'local': '本地库', 'share': '分享库'}
        lines, hit = [], set()
        for f in (folders if isinstance(folders, list) else []):
            for loc in (f.get('Locations') or []):
                loc_n = str(loc).replace('\\', '/').rstrip('/')
                lib = pm.lib_of(loc_n, fallback=False)
                if lib:
                    tag = f' ← 匹配{names[lib]}'
                    hit.add(lib)
                else:
                    # 媒体库选的是上级目录（一个库里同时含本地/分享）也算对上
                    inner = [k for k, root in (('local', pm.local), ('share', pm.share))
                             if root and loc_n and root.startswith(loc_n + '/')]
                    tag = (' ← 包含' + '、'.join(names[k] for k in inner)) if inner else ''
                    hit.update(inner)
                lines.append(f"  {f.get('Name') or '?'}: {loc}{tag}")
        if not lines:
            return ''
        out = '\n\nEmby 媒体库路径:\n' + '\n'.join(lines)
        for lib, root in (('local', pm.local), ('share', pm.share)):
            if lib not in hit:
                out += f"\n⚠️ 当前{names[lib]}路径 {root or '(未设置)'} 未对上任何媒体库，请从上面复制正确的路径"
        return out
    except Exception:
        return ''


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
        text = '✅ <b>TTD Guard</b> 测试消息\n如果你看到这条消息，说明 Telegram 通知已配置成功。'
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


@app.post('/api/emby/series/{series_id}/delete', dependencies=[Depends(auth)])
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


@app.post('/api/emby/series/{series_id}/resync', dependencies=[Depends(auth)])
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


@app.post('/api/emby/movie/delete_by_tmdb', dependencies=[Depends(auth)])
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


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=8321)
