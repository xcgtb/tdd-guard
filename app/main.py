# -*- coding: utf-8 -*-
"""Media Agent Web 层 —— FastAPI 后端"""
import os, sys, time, uuid, json, logging, threading, traceback, secrets, hmac, hashlib
import urllib.parse, urllib.request
from pathlib import Path
from fastapi import FastAPI, HTTPException, Depends, Request
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

app = FastAPI(title='TTD Guard', version='1.3.0')

TASKS = {}
TASK_LOCK = threading.Lock()
CURRENT = {'task': None}




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
    from pathlib import Path as _P
    _logger = logging.getLogger('media_agent')
    counter_file = _P('/data/.cd2_retry_count')

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


bot.set_current_ref(CURRENT)
try:
    bot.start()
except Exception as _e:
    logging.getLogger('media_agent').warning('Bot 启动失败: %s', _e)

# CD2 启动守门员：仅在显式开启时运行。
# daemon=True 不影响正常 Docker 运行，但允许测试/工具仅导入 app.main 后正常退出。
if os.environ.get('ENABLE_CD2_WATCHDOG', '0').strip().lower() in ('1', 'true', 'yes', 'on'):
    threading.Thread(target=_cd2_watchdog, daemon=True, name='cd2-watchdog').start()

try:
    _n = logger.migrate_legacy()
    if _n:
        logging.getLogger('media_agent').info('迁移旧日志 %d 条到 JSONL', _n)
except Exception as _e:
    logging.getLogger('media_agent').warning('日志迁移失败: %s', _e)


_security = HTTPBasic(auto_error=False)

# ── Cookie 会话登录 ──
# 原来只有浏览器原生 Basic 弹框，手机 Safari 经常不记住，每次都要重输。
# 现在登录一次后写入 30 天有效的签名 Cookie；Basic 头依然兼容（脚本/curl 可继续用）。
SESSION_COOKIE = 'tdd_session'
SESSION_DAYS = int(os.environ.get('SESSION_DAYS', '30') or 30)
_SESSION_KEY = (os.environ.get('SESSION_SECRET', '').strip() or WEB_PASSWORD or 'noauth').encode()


def _sign_session(user: str, exp: int) -> str:
    msg = ('%s|%d' % (user, exp)).encode()
    sig = hmac.new(_SESSION_KEY, msg, hashlib.sha256).hexdigest()
    return '%s|%d|%s' % (user, exp, sig)


def _verify_session(token: str) -> bool:
    try:
        user, exp, sig = token.split('|')
        if int(exp) < time.time():
            return False
        good = hmac.new(_SESSION_KEY, ('%s|%s' % (user, exp)).encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good) and hmac.compare_digest(user, WEB_USER)
    except Exception:
        return False


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
    time.sleep(1)  # 简单减缓暴力猜测
    return JSONResponse({'status': 'error', 'detail': '用户名或密码错误'}, status_code=401)


@app.post('/api/logout')
def api_logout():
    resp = JSONResponse({'status': 'success'})
    resp.delete_cookie(SESSION_COOKIE, path='/')
    return resp


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


class _StderrRouter:
    """按线程分流 stderr：worker 线程的 print/traceback 写入各自任务日志，
    其它线程（比如 uvicorn 处理别的请求时打印的异常）继续写真实 stderr，
    避免一个进程级的 sys.stderr 替换把不相关线程的输出混进任务日志里。"""
    def __init__(self, real):
        self._real = real
        self._lock = threading.Lock()
        self._routes = {}

    def register(self, stream):
        with self._lock:
            self._routes[threading.get_ident()] = stream

    def unregister(self):
        with self._lock:
            self._routes.pop(threading.get_ident(), None)

    def _target(self):
        return self._routes.get(threading.get_ident(), self._real)

    def write(self, s):
        self._target().write(s)

    def flush(self):
        self._target().flush()

    def isatty(self):
        return False


_STDERR_ROUTER = _StderrRouter(sys.stderr)
sys.stderr = _STDERR_ROUTER


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


TASKS_MAX_KEEP = 200  # 完成态任务最多保留这么多份，超出的按时间淘汰最旧的


def _prune_tasks():
    """TASKS 只增不减会在长期运行的容器里无限堆积，这里做个简单的上限淘汰：
    只清理已经跑完的任务（running 状态的，包括当前正在跑的，永远不动）。"""
    with TASK_LOCK:
        done = sorted(
            (t for t in TASKS.values() if t.status != 'running'),
            key=lambda t: t.ts,
        )
        overflow = len(done) - TASKS_MAX_KEEP
        if overflow > 0:
            for t in done[:overflow]:
                TASKS.pop(t.id, None)


def spawn(kind, fn, *fargs):
    with TASK_LOCK:
        cur = CURRENT['task']
        if cur is not None and cur.status == 'running':
            raise HTTPException(429, f'已有任务 [{cur.kind}] 正在执行，请稍候')
    _prune_tasks()
    t = Task(kind); TASKS[t.id] = t

    def worker():
        with TASK_LOCK: CURRENT['task'] = t
        _STDERR_ROUTER.register(t.stream)
        handler = logging.StreamHandler(t.stream)
        handler.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
        engine.log.addHandler(handler)
        try:
            res = fn(*fargs)
            t.result = res
            # busy（跨入口文件锁被占用）也按失败处理，否则网页会当成"执行成功"，显示 0 项/undefined
            t.status = 'error' if isinstance(res, dict) and res.get('status') in ('error', 'busy') else 'success'
            if t.status == 'error': t.error = res.get('message', '未知错误')
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            t.error = f'{type(e).__name__}: {e}'; t.status = 'error'
        finally:
            engine.log.removeHandler(handler); _STDERR_ROUTER.unregister()
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


# ═══════════════════ 双库治理：定时巡检（只扫描+通知，不自动清理） ═══════════════════
GOV_AUTO_FILE = engine.DATA_DIR / 'gov_auto.json'
_GOV_AUTO_DEFAULT = {
    'enabled': False,
    'interval_hours': 6,
    'notify_when_clean': False,
    'only_on_change': True,
    'last_run': 0,
    'last_status': '',
    'last_sig': '',
}
_gov_auto_lock = threading.Lock()
_gov_auto_running = {'v': False}


def _gov_auto_load() -> dict:
    d = dict(_GOV_AUTO_DEFAULT)
    try:
        d.update(json.loads(GOV_AUTO_FILE.read_text(encoding='utf-8')))
    except (OSError, ValueError):
        pass
    return d


def _gov_auto_save(d: dict):
    with _gov_auto_lock:
        try:
            engine.DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = GOV_AUTO_FILE.with_suffix('.tmp')
            tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding='utf-8')
            tmp.replace(GOV_AUTO_FILE)
        except OSError as e:
            engine.log.warning('保存治理巡检设置失败: %s', e)


def _gov_auto_sig(res: dict) -> str:
    keys = []
    for k in ('del_local_items', 'del_share_items'):
        for it in res.get(k) or []:
            keys.append('%s|%s|%s' % (k, it.get('title', ''), it.get('text', '')))
    keys.sort()
    return hashlib.sha1('\n'.join(keys).encode('utf-8')).hexdigest()


def _gov_auto_run():
    """后台线程：执行一次静默扫描，按设置决定是否推送 Telegram。"""
    try:
        try:
            t = spawn('inter_check', engine.ACTIONS['inter_check'], Args(silent=True))
        except HTTPException:
            engine.log.info('治理巡检：已有任务在执行，稍后重试')
            return
        while t.status == 'running':
            time.sleep(2)
        cfg = _gov_auto_load()
        cfg['last_run'] = time.time()
        if t.status != 'success' or not isinstance(t.result, dict):
            cfg['last_status'] = '扫描失败: %s' % (t.error or '未知错误')
            _gov_auto_save(cfg)
            bot.notify_auto_scan(None, error=cfg['last_status'])
            return
        res = t.result
        total = res.get('total_clean_cnt', 0)
        sig = _gov_auto_sig(res) if total else ''
        cfg['last_status'] = '待清理本地 %d / 待淘汰分享 %d' % (res.get('del_local_cnt', 0), res.get('del_share_cnt', 0))
        should = False
        if total > 0:
            should = (not cfg.get('only_on_change')) or sig != cfg.get('last_sig', '')
        elif cfg.get('notify_when_clean'):
            should = True
        if should and bot.notify_auto_scan(res):
            cfg['last_sig'] = sig
        elif total == 0:
            cfg['last_sig'] = ''
        _gov_auto_save(cfg)
    except Exception as e:
        engine.log.warning('治理巡检异常: %s', e)
    finally:
        _gov_auto_running['v'] = False


def _gov_auto_tick(now: float):
    if _gov_auto_running['v']:
        return
    cfg = _gov_auto_load()
    if not cfg.get('enabled'):
        return
    try:
        interval = max(1, min(168, int(cfg.get('interval_hours') or 6))) * 3600
    except (ValueError, TypeError):
        interval = 6 * 3600
    if now - float(cfg.get('last_run') or 0) < interval:
        return
    _gov_auto_running['v'] = True
    threading.Thread(target=_gov_auto_run, daemon=True, name='gov-auto').start()


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

            # ── 双库治理定时巡检 ──
            try:
                _gov_auto_tick(now)
            except Exception as e:
                engine.log.warning('治理巡检调度失败: %s', e)

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
    return {'status': 'ok', 'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'tz': engine.tz_info(),
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
        l_count, s_count = engine._get_strm_counts()
        # Emby 探测缓存 30 秒
        now_ts = time.time()
        if not hasattr(dashboard, '_emby_cache'):
            dashboard._emby_cache = {'ts': 0, 'ok': False, 'host': ''}
        if now_ts - dashboard._emby_cache['ts'] > 30:
            try:
                emby_ok = bool(engine.emby_request('/System/Info', timeout=3))
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
        if d == 'balanced':
            d = 'quality_first'
        if d not in ('quality_first', 'keep_local', 'keep_share'):
            raise HTTPException(400, 'decision 必须是 quality_first / keep_local / keep_share')
        kwargs['decision'] = d
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
def _gov_auto_view() -> dict:
    d = _gov_auto_load()
    last = float(d.get('last_run') or 0)
    interval = max(1, int(d.get('interval_hours') or 6)) * 3600
    return {
        'enabled': bool(d['enabled']),
        'interval_hours': int(d['interval_hours']),
        'notify_when_clean': bool(d['notify_when_clean']),
        'only_on_change': bool(d['only_on_change']),
        'last_run': last,
        'last_status': d.get('last_status', ''),
        'next_run': (last + interval) if (d['enabled'] and last) else 0,
        'running': _gov_auto_running['v'],
    }


@app.get('/api/governance/auto', dependencies=[Depends(auth)])
def api_get_gov_auto():
    return {'status': 'success', 'settings': _gov_auto_view()}


@app.post('/api/governance/auto', dependencies=[Depends(auth)])
def api_set_gov_auto(body: dict = None):
    body = body or {}
    d = _gov_auto_load()
    if 'enabled' in body:
        d['enabled'] = bool(body['enabled'])
    if 'interval_hours' in body:
        try:
            d['interval_hours'] = max(1, min(168, int(body['interval_hours'])))
        except (ValueError, TypeError):
            d['interval_hours'] = 6
    if 'notify_when_clean' in body:
        d['notify_when_clean'] = bool(body['notify_when_clean'])
    if 'only_on_change' in body:
        d['only_on_change'] = bool(body['only_on_change'])
    _gov_auto_save(d)
    return {'status': 'success', 'settings': _gov_auto_view()}


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
        items = data.get('Items') or []
        files = []
        for ep in items:
            conv = engine.emby_path_to_container(ep.get('Path') or '')
            if not conv:
                continue
            in_local = engine._inside(conv, engine.L_ROOT)
            in_share = engine._inside(conv, engine.S_ROOT)
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

        # 通知 Emby 刷新媒体库
        try:
            engine.notify_emby_refresh()
        except Exception:
            pass

        # 清空分集/索引缓存，保证下次查这部剧能拿到最新数据
        try:
            engine._ep_cache['ts'] = 0
            engine._ep_cache['data'] = None
            engine._emby_index_cache['ts'] = 0
            engine._emby_index_cache['data'] = None
            engine._invalidate_lib_cache()
            engine.invalidate_stats_cache()
        except Exception:
            pass

        # 片库映射页用的是已对照过 TMDB 的持久化缓存（_emby_lib_cache / 磁盘文件），
        # 之前这里会整体清空，导致下次打开片库映射时所有剧集的 TMDB 对照结果
        # 都被打回"待对照"，只能整页重新跑一遍很慢的 TMDB 对照。
        # 这里改为只在缓存里"就地"更新/移除这一部剧，其余剧集的对照结果保留。
        try:
            engine.patch_emby_lib_cache_after_series_delete(series_id, target)
        except Exception:
            pass

        target_cn = '本地库' if target == 'local' else '分享库'
        cloud_note = '未处理云端源文件' if target == 'share' else ('删除 %d 个云端源文件' % result['cloud_removed'])
        engine.write_audit_log(
            '单剧删除',
            '《%s》删除%s：%d 个 strm' % (series_name, target_cn, result['strm_removed']),
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
        }
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

    if not tmdb_id:
        return {'status': 'error', 'message': '缺少 tmdb_id'}
    if target not in ('local', 'share'):
        return {'status': 'error', 'message': 'target 必须是 local 或 share'}

    root = engine.L_ROOT if target == 'local' else engine.S_ROOT
    cloud = engine.CLOUD_L_ROOT if target == 'local' else None

    if not root.exists():
        return {'status': 'success', 'count': 0, 'target': target}

    # 匹配 {tmdb-xxx} / {tmdb_xxx} / tmdb-xxx 三种格式
    marks = ['{tmdb-%s}' % tmdb_id, '{tmdb_%s}' % tmdb_id, 'tmdb-%s' % tmdb_id]
    files = []
    for entry in root.rglob('*'):
        if not entry.is_dir():
            continue
        name = entry.name
        if any(m in name for m in marks):
            for f in entry.rglob('*.strm'):
                files.append(f)

    if not files:
        return {'status': 'success', 'count': 0, 'target': target}

    if dry_run:
        return {'status': 'success', 'dry_run': True, 'count': len(files), 'target': target}

    result = engine.safe_delete_files(files, root, cloud, dry_run=False)

    try:
        engine.notify_emby_refresh()
    except Exception:
        pass

    try:
        engine._ep_cache['ts'] = 0
        engine._ep_cache['data'] = None
        engine._emby_lib_cache['ts'] = 0
        engine._emby_lib_cache['data'] = None
        engine._emby_index_cache['ts'] = 0
        engine._emby_index_cache['data'] = None
        engine._invalidate_lib_cache()
        engine.invalidate_stats_cache()
    except Exception:
        pass

    target_cn = '本地库' if target == 'local' else '分享库'
    cloud_note = '未处理云端源文件' if target == 'share' else ('删除 %d 个云端源文件' % result['cloud_removed'])

    engine.write_audit_log(
        '单剧删除',
        '《%s》删除%s：%d 个 strm' % (movie_name, target_cn, result['strm_removed']),
        [
            '类型：电影',
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
    }

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=8321)