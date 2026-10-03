#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TDD Guard 全量诊断脚本（手动 / CI 用，不是容器健康检查）
覆盖：Python 语法 / import / 关键函数 / 依赖 / 配置文件 / 路径 / 前端 HTML 结构
用法：
    # 宿主机跑（只能验证 Python 语法）
    python3 scripts/diagnose.py
    # 容器内跑（推荐，能验证依赖 / 路由 / 配置）
    docker compose exec ttd-guard python /app/scripts/diagnose.py

注意：容器的 Docker HEALTHCHECK 用的是同目录下的 healthcheck.py（轻量存活探针，
只探测 /api/health），不要把这个脚本接回 HEALTHCHECK —— 它和前端具体的
JS 函数名/元素 id 强耦合，前端重构时很容易把容器误判为 unhealthy。
"""
import os, sys, re, ast, json, importlib, pathlib, io, traceback

# ── 是否在容器内跑：容器内没有 Dockerfile / docker-compose.yml，跳过对应检查 ──
IN_CONTAINER = os.path.exists('/.dockerenv')

# ── 项目根目录自动检测：兼容 scripts/diagnose.py 和根目录下 diagnose.py 两种摆放 ──
_here = pathlib.Path(__file__).parent.resolve()
if (_here / 'app').is_dir() and (_here / 'static').is_dir():
    ROOT = _here
elif (_here.parent / 'app').is_dir() and (_here.parent / 'static').is_dir():
    ROOT = _here.parent
else:
    ROOT = _here

APP_DIR = ROOT / 'app'
STATIC_DIR = ROOT / 'static'

OK   = '\033[32m✅\033[0m'
FAIL = '\033[31m❌\033[0m'
WARN = '\033[33m⚠️ \033[0m'

results = {'ok': 0, 'fail': 0, 'warn': 0}
errors_detail = []


def ok(msg):
    results['ok'] += 1
    print(f'  {OK} {msg}')


def fail(msg, detail=None):
    results['fail'] += 1
    print(f'  {FAIL} {msg}')
    if detail:
        print(f'     {detail}')
        errors_detail.append((msg, detail))


def warn(msg, detail=None):
    results['warn'] += 1
    print(f'  {WARN}{msg}')
    if detail:
        print(f'     {detail}')


def skip(msg):
    print(f'  \033[90m⏭  {msg}（容器内跑，跳过）\033[0m')


def section(title):
    print(f'\n\033[1;36m━━━ {title} ━━━\033[0m')


# ═══════════════════ 1. 文件结构 ═══════════════════
section('1. 文件结构')

# 容器内不需要这两个文件（它们只在宿主机上，容器里跑服务用不到）
host_only_files = ['Dockerfile', 'docker-compose.yml']
required_files = [
    'requirements.txt',
    'app/__init__.py',
    'app/core.py',
    'app/config.py',
    'app/engine.py',
    'app/bot.py',
    'app/main.py',
    'app/logger.py',
    'static/index.html',
]

if IN_CONTAINER:
    for rel in host_only_files:
        skip(rel)
else:
    required_files = host_only_files + required_files

for rel in required_files:
    p = ROOT / rel
    if p.exists() and p.is_file():
        size = p.stat().st_size
        if size == 0 and rel != 'app/__init__.py':
            warn(f'{rel} 存在但大小为 0')
        else:
            ok(f'{rel}  ({size} 字节)')
    else:
        fail(f'{rel} 缺失')

# healthcheck.py：可能在 scripts/ 下，也可能在根目录
hc = ROOT / 'scripts' / 'healthcheck.py'
if not hc.exists():
    hc = ROOT / 'healthcheck.py'
if hc.exists():
    ok(f'{hc.relative_to(ROOT)}  ({hc.stat().st_size} 字节)')
else:
    fail('scripts/healthcheck.py 缺失')


# ═══════════════════ 2. Python 语法 ═══════════════════
section('2. Python 语法检查')

py_files = sorted(APP_DIR.glob('*.py'))
hc2 = ROOT / 'scripts' / 'healthcheck.py'
if hc2.exists():
    py_files.append(hc2)
else:
    alt = ROOT / 'healthcheck.py'
    if alt.exists():
        py_files.append(alt)

for f in py_files:
    try:
        src = f.read_text(encoding='utf-8')
        ast.parse(src, filename=str(f))
        ok(f'{f.relative_to(ROOT)} 语法正确')
    except SyntaxError as e:
        fail(f'{f.relative_to(ROOT)} 语法错误', f'第 {e.lineno} 行: {e.msg}')
    except Exception as e:
        fail(f'{f.relative_to(ROOT)} 读取失败', str(e))


# ═══════════════════ 3. 依赖包 ═══════════════════
section('3. 依赖包')

deps = ['fastapi', 'uvicorn', 'starlette', 'pydantic']
for mod in deps:
    try:
        m = importlib.import_module(mod)
        ver = getattr(m, '__version__', '?')
        ok(f'{mod} {ver}')
    except ImportError:
        if IN_CONTAINER:
            fail(f'{mod} 未安装')
        else:
            skip(f'{mod}（宿主机没装，容器里有，正常）')


# ═══════════════════ 4. app 内部模块 import ═══════════════════
section('4. app 内部模块 import')

sys.path.insert(0, str(ROOT))

try:
    from app import core
    ok('core 导入成功')
except Exception as e:
    fail('core 导入失败', traceback.format_exc())


# ═══════════════════ 5. core 关键函数 ═══════════════════
section('5. core 关键函数')

if 'app.core' in sys.modules:
    from app import core
    core_funcs = [
        'esc', 'parse_season_dir', 'get_ep', 
        'title_key', 'is_exempt', 'fmt_nums',
        'analyze_season_episodes', 'is_seq', 'parse_emby_library',
    ]
    for fn in core_funcs:
        if hasattr(core, fn):
            ok(f'core.{fn}')
        else:
            fail(f'core.{fn} 缺失')

    try:
        assert core.parse_season_dir('S01') == 1
        assert core.parse_season_dir('Specials') == 0
        assert core.parse_season_dir('第2季') == 2
        assert core.get_ep('Show.S01E05.mkv') == (1, 5)
        assert core.get_score('x.DV.2160p.mkv') > core.get_score('x.1080p.mkv')
        assert core.analyze_season_episodes([1, 2, 3])[0] is True
        assert core.analyze_season_episodes([1, 3])[0] is False
        ok('core 冒烟测试通过')
    except AssertionError as e:
        fail('core 冒烟测试失败', str(e))


# ═══════════════════ 6. config 模块 ═══════════════════
section('6. config 模块')

try:
    from app import config
    cfg_funcs = [
        'load_config', 'save_config',
        'get_strategy', 'update_strategy',
        'get_subscriptions', 'set_subscriptions',
        'get_morning_report', 'update_morning_report',
        'get_ingest_cfg', 'mask_value', 'is_masked_value',
    ]
    for fn in cfg_funcs:
        if hasattr(config, fn):
            ok(f'config.{fn}')
        else:
            fail(f'config.{fn} 缺失')

    cfg = config.load_config()
    ok(f'load_config OK (emby={cfg.get("emby_host", "?")})')

    masked = config.mask_value('abcd1234efgh5678')
    if '****' in masked:
        ok(f'mask_value OK → {masked}')
    else:
        fail(f'mask_value 异常: {masked}')

    strat = config.get_strategy()
    for k in ('decision', 'multi_season_protect', 'tie_keep_local',
              'exempt_keywords', 'special_action'):
        if k in strat:
            ok(f'strategy.{k} = {strat[k]}')
        else:
            fail(f'strategy 缺字段: {k}')
except Exception as e:
    fail('config 模块异常', traceback.format_exc())


# ═══════════════════ 7. engine 模块 ═══════════════════
section('7. engine 模块')

os.environ.setdefault('EMBY_HOST', 'http://127.0.0.1:8096')

try:
    from app import engine
    ok('engine 导入成功')

    engine_funcs = [
        'emby_request', 'notify_telegram',
        'Lib', 'build_plan', 'save_plan',
        'action_inter_check', 'action_inter_clean',
        'action_stats', 'action_played', 'action_search', 'action_logs',
        'action_explore', 'action_emby_library',
        'refresh_ingest_cache', 'read_ingest_cache', 'get_ingest',
        'scan_exempt_matches', '_exempt_keywords', '_strategy',
        'check_subscriptions', 'build_morning_report', 'send_morning_report',
        'Tmdb', 'TmdbError',
        '_load_sub_state', '_save_sub_state',
        '_get_lib', '_invalidate_lib_cache',
    ]
    for fn in engine_funcs:
        if hasattr(engine, fn):
            ok(f'engine.{fn}')
        else:
            fail(f'engine.{fn} 缺失')

    required_actions = [
        'inter_check', 'inter_clean', 'stats', 'played',
        'search', 'logs', 'explore', 'emby_library',
    ]
    for a in required_actions:
        if a in engine.ACTIONS:
            ok(f'ACTIONS[{a}]')
        else:
            fail(f'ACTIONS 缺 {a}')

    for const in ('L_ROOT', 'S_ROOT', 'CLOUD_L_ROOT', 'DATA_DIR'):
        if hasattr(engine, const):
            p = getattr(engine, const)
            if p.exists():
                ok(f'{const} = {p}  (存在)')
            else:
                warn(f'{const} = {p}  (不存在，容器外测试正常)')

    cache = engine.read_ingest_cache()
    if cache:
        st = cache.get('stats') or {}
        ok(f'入库缓存 OK  电影={st.get("movies", 0)}  剧集={st.get("series", 0)}/{st.get("episodes", 0)}')
    else:
        warn('入库缓存为空（首次启动正常）')

    sub_state = engine._load_sub_state()
    ok(f'订阅状态 OK  ({len(sub_state)} 条记录)')

except Exception as e:
    fail('engine 模块异常', traceback.format_exc())


# ═══════════════════ 8. bot 模块 ═══════════════════
section('8. bot 模块')

try:
    from app import bot
    bot_funcs = [
        'start', 'stop', 'restart', 'status', 'set_current_ref',
        '_send', '_edit', '_try_delete', '_schedule_delete',
        '_handle_command', '_handle_callback', '_notify_result',
    ]
    for fn in bot_funcs:
        if hasattr(bot, fn):
            ok(f'bot.{fn}')
        else:
            fail(f'bot.{fn} 缺失')

    st = bot.status()
    ok(f'bot.status OK  running={st.get("running")}  username={st.get("bot_username") or "未连接"}')
except Exception as e:
    fail('bot 模块异常', traceback.format_exc())


# ═══════════════════ 9. main 模块 ═══════════════════
section('9. main 模块')

try:
    from app import main
    ok('main 导入成功')

    routes = [r.path for r in main.app.routes]
    required_routes = [
        '/api/health', '/api/dashboard', '/api/check', '/api/clean',
        '/api/ingest', '/api/ingest/status', '/api/ingest/settings',
        '/api/stats', '/api/played', '/api/search', '/api/logs',
        '/api/records',
        '/api/explore', '/api/emby/library', '/api/emby/poster/{item_id}',
        '/api/strategy', '/api/exempt/scan',
        '/api/subscriptions', '/api/subscriptions/settings', '/api/subscriptions/check',
        '/api/morning', '/api/morning/preview', '/api/morning/send',
        '/api/config', '/api/config/test/emby', '/api/config/test/tmdb',
        '/api/config/test/telegram', '/api/bot/status',
        '/api/task/{tid}', '/api/tmdb/progress',
    ]
    for r in required_routes:
        if r in routes:
            ok(f'路由 {r}')
        else:
            fail(f'路由 {r} 缺失')
except Exception as e:
    if IN_CONTAINER:
        fail('main 模块异常', traceback.format_exc())
    else:
        skip(f'main 模块（宿主机缺 fastapi 等依赖，容器里正常）')


# ═══════════════════ 10. 前端 HTML 结构 ═══════════════════
section('10. 前端 HTML')

html_path = STATIC_DIR / 'index.html'
if html_path.exists():
    html = html_path.read_text(encoding='utf-8')
    size = len(html)

    if size > 50000:
        ok(f'index.html  ({size} 字节)')
    else:
        warn(f'index.html 偏小 ({size} 字节)，可能不完整')

    for tag in ('<html', '<head', '<body', '</html>', '<script', '</script>', '<style', '</style>'):
        if tag in html:
            ok(f'包含 {tag}')
        else:
            fail(f'缺少 {tag}')

    js_funcs = [
        'function api', 'function injectIcons', 'function switchTab',
        'function loadDashboard', 'function scanLibrary', 'function runClean',
        'function loadExplore', 'function loadEmbyLibrary',
        'function loadSubscriptions', 'function loadMorning',
        'function loadStats', 'function loadRecords',
        'function loadConfig', 'function loadBotStatus',
        'function loadStrategy', 'function loadIngest',
        'function pickStrategy', 'function pickSpecial',
        'function addExempt', 'function scanExempt',
        'function toggleReveal', 'function saveConfig',
        'function saveSubSettings', 'function saveMorning',
        'function saveIngest', 'function saveStrategy',
        'function checkSubsNow', 'function sendMorningNow',
        'function previewMorning', 'function toggleSubscribe',
        'function showGovDetailAt', 'function showGovDetail',
        'function hydratePosters',
    ]
    for fn in js_funcs:
        if fn in html:
            ok(f'JS {fn}')
        else:
            fail(f'JS {fn} 缺失')

    ids = [
        'tab-dashboard', 'tab-explore', 'tab-governance', 'tab-mapping',
        'tab-subscribe', 'tab-morning', 'tab-daily', 'tab-history', 'tab-settings',
        'stat-local', 'stat-share', 'svc-emby', 'svc-tmdb',
        'stat-sub', 'stat-morning', 'stat-ingest-mov', 'stat-ingest-age',
        'planList', 'govFilter',
        'subList', 'subCount', 'subEnabled', 'subCheckTmdb', 'subInterval',
        'mrEnabled', 'mrHour', 'mrMinute', 'mrPrescan',
        'mrItemStats', 'mrItemSubs', 'mrItemGap',
        'statsOut', 'statsCacheHint',
        'exemptInput', 'exemptList', 'exemptMatches',
        'ingEnabled', 'ingInterval', 'ingStatus',
        'cfg-emby-host', 'cfg-emby-key', 'cfg-tmdb-key',
        'cfg-tg-token', 'cfg-tg-chat', 'cfg-tg-users',
        'botStatus', 'recordN', 'recordsList',
    ]
    missing_ids = [i for i in ids if f'id="{i}"' not in html]
    if not missing_ids:
        ok(f'所有 {len(ids)} 个关键 id 都齐全')
    else:
        for i in missing_ids:
            fail(f'id="{i}" 缺失')

    # 遗留 48 文案
    leftover_48 = re.findall(r'(48\s*小时|48小时|48H|48h)', html)
    if leftover_48:
        fail(f'前端仍有遗留 48 文案: {leftover_48}')
    else:
        ok('前端无遗留 48 文案')

    # 硬编码内网地址：只查 value/href/src 属性，忽略 placeholder（那是示例提示，无害）
    hardcoded = sorted(set(re.findall(
        r'(?:value|href|src)\s*=\s*["\']?(https?://(?:192\.168|10\.|172\.(?:1[6-9]|2\d|3[01]))[\d.]*:\d+)',
        html
    )))
    if hardcoded:
        fail(f'前端有硬编码内网地址: {hardcoded}')
    else:
        ok('前端无硬编码内网地址')

else:
    fail('index.html 不存在')


# ═══════════════════ 11. 配置文件 ═══════════════════
section('11. 配置文件')

data_dir = pathlib.Path(os.environ.get('AGENT_DATA', str(ROOT)))
cfg_file = data_dir / 'config.json'

if cfg_file.exists():
    try:
        cfg = json.loads(cfg_file.read_text(encoding='utf-8'))
        ok(f'config.json 合法  ({len(cfg)} 个字段)')

        for k in ('emby_host', 'emby_key', 'tmdb_key',
                  'telegram_bot_token', 'telegram_chat_id'):
            if k in cfg and cfg[k]:
                ok(f'config.{k} 已配置')
            else:
                warn(f'config.{k} 为空')

        for k in ('strategy_decision', 'strategy_multi_season_protect',
                  'strategy_special_action', 'exempt_keywords'):
            if k in cfg:
                ok(f'config.{k} = {cfg[k]}')
    except json.JSONDecodeError as e:
        fail('config.json 格式错误', str(e))
else:
    warn(f'{cfg_file} 不存在（首次启动会用默认值生成）')

state_dir = data_dir / 'state'
if state_dir.exists():
    files = list(state_dir.glob('*'))
    ok(f'state 目录存在，{len(files)} 个文件')
    for name in ('ingest_cache.json', 'subscriptions_state.json', 'tmdb_cache.json'):
        f = state_dir / name
        if f.exists():
            try:
                json.loads(f.read_text(encoding='utf-8'))
                ok(f'{name} 合法')
            except json.JSONDecodeError:
                fail(f'{name} 格式错误')
else:
    warn('state 目录不存在')


# ═══════════════════ 12. Docker 配置 ═══════════════════
section('12. Docker 配置')

if IN_CONTAINER:
    skip('Dockerfile / docker-compose.yml 检查')
else:
    df = ROOT / 'Dockerfile'
    if df.exists():
        txt = df.read_text()
        for kw in ('COPY app', 'COPY static', 'COPY requirements', 'CMD'):
            if kw in txt:
                ok(f'Dockerfile 含 {kw}')
            else:
                fail(f'Dockerfile 缺 {kw}')

        m = re.search(r'CMD\s*\[(.*?)\]', txt, re.DOTALL)
        if m:
            cmd = m.group(1)
            if 'app.main:app' in cmd:
                ok('CMD 正确: app.main:app')
            else:
                fail(f'CMD 路径异常: {cmd.strip()}')
    else:
        fail('Dockerfile 缺失')

    dc = ROOT / 'docker-compose.yml'
    if dc.exists():
        txt = dc.read_text()
        for kw in ('ttd-guard', 'WEB_PASSWORD', '/data'):
            if kw in txt:
                ok(f'compose 含 {kw}')
            else:
                warn(f'compose 缺 {kw}')
        if 'WEB_PASSWORD=change-me-please' in txt:
            warn('compose 里 WEB_PASSWORD 还是默认值 change-me-please，建议改掉')
    else:
        fail('docker-compose.yml 缺失')


# ═══════════════════ 13. 编码检查 ═══════════════════
section('13. 编码 / 字符检查')

for f in py_files:
    try:
        src = f.read_bytes()
        if src.startswith(b'\xef\xbb\xbf'):
            warn(f'{f.relative_to(ROOT)} 含 UTF-8 BOM')
        src.decode('utf-8')
    except UnicodeDecodeError as e:
        fail(f'{f.relative_to(ROOT)} 编码不是 UTF-8', str(e))

ok('所有 Python 文件编码检查通过')


# ═══════════════════ 汇总 ═══════════════════
print('\n')
print('\033[1;36m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m')
print(f'\033[1m  汇总  {OK} {results["ok"]}    {FAIL} {results["fail"]}    {WARN} {results["warn"]}\033[0m')
print('\033[1;36m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m')

if results['fail'] == 0:
    print(f'\n{OK} 全部通过！系统健康。\n')
    sys.exit(0)
else:
    print(f'\n{FAIL} 有 {results["fail"]} 项失败：\n')
    for msg, detail in errors_detail[:20]:
        print(f'  • {msg}')
        if detail:
            print(f'    {detail}')
    sys.exit(1)