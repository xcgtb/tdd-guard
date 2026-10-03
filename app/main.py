# -*- coding: utf-8 -*-
"""Media Agent Web 层 —— FastAPI 应用装配。

v1.7.7：约 40 个路由按页面域拆到 app/routers/（auth/system/governance/library/subscribe/settings），
本文件只保留：启动密码检查（deps）、CD2 看门狗、应用生命周期、静态挂载与路由装配。
main.app / main.auth / main.engine 等旧导入路径全部保留（tests、scripts/diagnose.py 在用）。
"""
import os, sys, logging, threading
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import state, storage, bot, logger, scheduler, tasks  # noqa: F401
from app import engine  # noqa: F401  (兼容壳：仅为保留 main.engine 旧导入路径)
from app import config as _cfg  # noqa: F401
from app import security as _sec
from app.routers.deps import (  # noqa: F401
    auth, spawn, Args, STATIC_DIR,
    WEB_USER, WEB_PASSWORD, ALLOW_NO_AUTH, APP_VERSION,
    SESSION_COOKIE, SESSION_DAYS, _ALLOWED_HOSTS,
)
from app.routers import auth as _r_auth
from app.routers import system as _r_system
from app.routers import governance as _r_governance
from app.routers import library as _r_library
from app.routers import subscribe as _r_subscribe
from app.routers import settings as _r_settings


def _cd2_ready():
    """检查 CD2 是否就绪"""
    check_dirs = {'电影', '剧集', '儿童节目', '综艺', '动漫', '纪录片', '演唱会'}
    try:
        cd2 = state.CLOUD_L_ROOT
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
    counter_file = state.DATA_DIR / '.cd2_retry_count'

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
    # SQLite 存储层一次性迁移：既有计划/订阅状态/审计 JSON → 库（幂等）
    try:
        _m = storage.db_migrate()
        if any(_m.values()):
            _log.info('SQLite 存储层迁移完成: %s', _m)
    except Exception as e:
        _log.warning('存储层迁移失败（不影响启动）: %s', e)
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

@app.middleware('http')
async def _security_mw(request: Request, call_next):
    """CSRF 同源校验 + 安全响应头（v1.7.7 起）。"""
    if request.url.path.startswith('/api/') and _sec.csrf_blocked(
            request.method, request.headers, bool(request.cookies.get(SESSION_COOKIE)), _ALLOWED_HOSTS):
        return JSONResponse({'status': 'error', 'detail': '跨站请求被拒绝（Origin 校验失败）。'
                             '如通过反向代理访问，请设置 ALLOWED_ORIGINS。'}, status_code=403)
    resp = await call_next(request)
    for k, v in _sec.SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    return resp


if STATIC_DIR.exists():
    app.mount('/static', StaticFiles(directory=str(STATIC_DIR)), name='static')

# 路由装配：顺序无关（路径互不冲突），分组见 app/routers/__init__.py
app.include_router(_r_auth.router)
app.include_router(_r_system.router)
app.include_router(_r_governance.router)
app.include_router(_r_library.router)
app.include_router(_r_subscribe.router)
app.include_router(_r_settings.router)


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=8321)
