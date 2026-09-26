# -*- coding: utf-8 -*-
"""Telegram Bot —— 长轮询模式
特性：
  - 无底部常驻键盘，界面极简，靠 ☰ 命令菜单操作
  - 操作反馈卡片 60 秒自动销毁（阅后即焚）
  - 用户命令消息 10 秒自动删除
  - 欢迎提示 15 秒即焚，只保留最新一条
"""
import json, re, time, logging, threading, urllib.request, urllib.parse
from argparse import Namespace

try:
    from . import engine
    from . import config as _cfg
    from .config import load_config
except ImportError:
    import engine
    import config as _cfg
    from config import load_config

log = logging.getLogger('media_agent.bot')
_API = 'https://api.telegram.org/bot{token}/{method}'

_state = {
    'running': False, 'thread': None, 'offset': 0,
    'last_error': '', 'last_poll': 0, 'bot_username': '',
    'initialized': False,
}
_LOCK = threading.Lock()
_current_ref = {'obj': None}

_delete_queue = []
_DELETE_LOCK = threading.Lock()
BOT_MSG_TTL  = 60
USER_MSG_TTL = 10
MENU_MSG_TTL = 15

_last_menu_msg = {}


# ═══════════════════ 基础 ═══════════════════
def set_current_ref(ref):
    _current_ref['obj'] = ref


def _get_current_task():
    ref = _current_ref['obj']
    return ref.get('task') if ref else None


def _api(token, method, params=None, timeout=35):
    url = _API.format(token=token, method=method)
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(url, data=data, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


# ═══════════════════ 阅后即焚队列 ═══════════════════
def _schedule_delete(chat_id, message_id, delay):
    if not message_id: return
    with _DELETE_LOCK:
        _delete_queue[:] = [(d, c, m) for (d, c, m) in _delete_queue if m != message_id]
        _delete_queue.append((time.time() + delay, chat_id, message_id))


def _try_delete(token, chat_id, message_id):
    try:
        _api(token, 'deleteMessage',
             {'chat_id': str(chat_id), 'message_id': message_id}, timeout=10)
        return True
    except Exception as e:
        log.debug('删除消息失败 (%s/%s): %s', chat_id, message_id, e)
        return False


def _cleanup_loop():
    log.info('阅后即焚线程启动')
    while _state['running']:
        time.sleep(2)
        now = time.time()
        token = (load_config().get('telegram_bot_token') or '').strip()
        if not token: continue
        with _DELETE_LOCK:
            remaining = []
            for deadline, chat_id, mid in _delete_queue:
                if now >= deadline:
                    _try_delete(token, chat_id, mid)
                else:
                    remaining.append((deadline, chat_id, mid))
            _delete_queue[:] = remaining


# ═══════════════════ 发送 / 编辑 ═══════════════════
def _send(token, chat_id, text, keyboard=None, reply_to=None,
          ttl=None, delete_user_msg=None):
    params = {
        'chat_id': str(chat_id), 'text': text[:4000],
        'parse_mode': 'HTML', 'disable_web_page_preview': 'true',
    }
    if reply_to: params['reply_to_message_id'] = reply_to
    if keyboard is not None: params['reply_markup'] = json.dumps(keyboard)
    try:
        result = _api(token, 'sendMessage', params, timeout=15)
    except Exception as e:
        log.warning('Telegram 发送失败: %s', e)
        return None

    if result and result.get('ok'):
        mid = result.get('result', {}).get('message_id')
        if mid:
            delay = BOT_MSG_TTL if ttl is None else ttl
            if delay > 0:
                _schedule_delete(chat_id, mid, delay)
        if delete_user_msg:
            _schedule_delete(chat_id, delete_user_msg, USER_MSG_TTL)
    return result


def _answer_callback(token, cb_id, text='', alert=False):
    try:
        _api(token, 'answerCallbackQuery', {
            'callback_query_id': cb_id, 'text': text[:200],
            'show_alert': 'true' if alert else 'false',
        }, timeout=10)
    except Exception as e:
        log.warning('应答按钮失败: %s', e)


def _edit(token, chat_id, message_id, text, keyboard=None):
    params = {
        'chat_id': str(chat_id), 'message_id': message_id,
        'text': text[:4000], 'parse_mode': 'HTML',
        'disable_web_page_preview': 'true',
    }
    if keyboard is not None: params['reply_markup'] = json.dumps(keyboard)
    try:
        result = _api(token, 'editMessageText', params, timeout=15)
        _schedule_delete(chat_id, message_id, BOT_MSG_TTL)
        return result
    except Exception as e:
        log.warning('编辑消息失败: %s', e)
        return None


def _is_allowed(user_id, chat_id):
    raw = (load_config().get('telegram_allowed_users') or '').strip()
    if not raw: return False
    allowed = set(x.strip() for x in raw.split(',') if x.strip())
    return str(user_id) in allowed or str(chat_id) in allowed


# ═══════════════════ 键盘 ═══════════════════
def _clear_keyboard():
    return {'remove_keyboard': True}


def _plan_keyboard(plan_id):
    return {'inline_keyboard': [
        [{'text': '📋 查看清单', 'callback_data': f'plan:{plan_id}'},
         {'text': '🗑️ 执行清理', 'callback_data': f'clean_ask:{plan_id}'}],
        [{'text': '❌ 忽略', 'callback_data': 'dismiss'}],
    ]}


def _clean_confirm_keyboard(plan_id):
    return {'inline_keyboard': [
        [{'text': '✅ 确认执行', 'callback_data': f'clean_do:{plan_id}'},
         {'text': '❌ 取消', 'callback_data': 'dismiss'}],
    ]}


# ═══════════════════ 命令注册 ═══════════════════
MY_COMMANDS = [
    {'command': 'menu',    'description': '🎛️ 显示菜单'},
    {'command': 'check',   'description': '⚖️ 扫描双库'},
    {'command': 'clean',   'description': '🗑️ 执行清理'},
    {'command': 'ingest',  'description': '📊 入库速报（/ingest force 强制刷新）'},
    {'command': 'stats',   'description': '📊 24H 入库（同 ingest）'},
    {'command': 'played',  'description': '▶️ 24H 播放记录'},
    {'command': 'emby',    'description': '📺 Emby 库总览'},
    {'command': 'gap',     'description': '🧩 缺集检测'},
    {'command': 'search',  'description': '🔍 搜索片名'},
    {'command': 'sub',     'description': '🔔 追更订阅'},
    {'command': 'morning', 'description': '☀️ 立即发送晨报'},
    {'command': 'logs',    'description': '📜 审计日志'},
    {'command': 'reset',   'description': '🔄 重置面板'},
    {'command': 'help',    'description': '⚙️ 帮助'},
]


def _init_bot(token):
    try:
        _api(token, 'setMyCommands', {
            'commands': json.dumps(MY_COMMANDS, ensure_ascii=False),
            'scope': json.dumps({'type': 'default'}),
        }, timeout=10)
        log.info('Bot 命令列表已注册')
    except Exception as e:
        log.warning('setMyCommands 失败: %s', e)


HELP_TEXT = (
    '🎬 <b>TDD Guard</b>\n'
    '━━━━━━━━━━━━━━━━━━\n'
    '点输入框左侧 <b>☰</b> 或直接输命令：\n\n'
    '<b>治理</b>\n'
    '• /check — 扫描双库\n'
    '• /clean — 清理最近计划\n'
    '<b>统计</b>\n'
    '• /ingest — 24H 入库（/ingest force 强制刷新）\n'
    '• /played — 24H 播放\n'
    '• /emby — Emby 库总览\n'
    '• /gap — 缺集检测\n'
    '<b>订阅 / 晨报</b>\n'
    '• /sub — 查看订阅 / /sub check 立即检查\n'
    '• /morning — 立即发送晨报\n'
    '<b>其它</b>\n'
    '• /search 关键词 — 搜片\n'
    '• /logs [n] — 审计日志\n'
    '• /menu /reset — 菜单\n'
    '• /help — 帮助\n'
    '\n💡 反馈卡片 60 秒后自动销毁'
)


# ═══════════════════ 任务派生 ═══════════════════
def _spawn_and_watch(kind, action_fn, chat_id, message_id=None,
                     user_msg_id=None, **kwargs):
    token = (load_config().get('telegram_bot_token') or '').strip()
    if not token: return
    args = Namespace(kw='', plan='', dry_run=False, **kwargs)

    def worker():
        try:
            res = action_fn(args)
            _notify_result(token, chat_id, kind, res, message_id, user_msg_id)
        except Exception as e:
            log.exception('Bot 任务失败')
            _send(token, chat_id, f'❌ {kind} 执行失败: {e}')

    threading.Thread(target=worker, daemon=True).start()


# ═══════════════════ 菜单 ═══════════════════
def _show_menu(token, chat_id, user_msg_id=None):
    old = _last_menu_msg.get(chat_id)
    if old:
        _try_delete(token, chat_id, old)
    m = _send(token, chat_id,
              '🎬 <b>TDD Guard</b>\n\n点击输入框左侧 <b>☰</b>，或输入 / 查看命令',
              _clear_keyboard(),
              ttl=MENU_MSG_TTL,
              delete_user_msg=user_msg_id)
    if m and m.get('ok'):
        _last_menu_msg[chat_id] = m.get('result', {}).get('message_id')


def _reset_menu(token, chat_id, user_msg_id=None):
    try:
        _api(token, 'sendMessage', {
            'chat_id': str(chat_id),
            'text': '🔄 面板已重置',
            'reply_markup': json.dumps({'remove_keyboard': True}),
        }, timeout=10)
    except Exception as e:
        log.warning('清空键盘失败: %s', e)
    _show_menu(token, chat_id, user_msg_id=user_msg_id)


# ═══════════════════ 动作分发 ═══════════════════
def _dispatch(action, token, chat_id, message_id=None, user_msg_id=None, arg=''):
    if action == 'menu':
        _show_menu(token, chat_id, user_msg_id=user_msg_id)

    elif action == 'help':
        if message_id: _edit(token, chat_id, message_id, HELP_TEXT)
        else: _send(token, chat_id, HELP_TEXT)

    elif action == 'status':
        cur = _get_current_task()
        s = f'⏳ 运行中: <b>{cur.kind}</b>' if (cur and cur.status == 'running') else '✅ 空闲中'
        if message_id: _edit(token, chat_id, message_id, s)
        else: _send(token, chat_id, s)

    elif action == 'check':
        if message_id:
            _edit(token, chat_id, message_id, '⏳ 正在扫描双库...', {'inline_keyboard': []})
            _spawn_and_watch('check', engine.ACTIONS['inter_check'], chat_id, message_id=message_id)
        else:
            m = _send(token, chat_id, '⏳ 正在扫描双库...')
            mid = (m or {}).get('result', {}).get('message_id')
            _spawn_and_watch('check', engine.ACTIONS['inter_check'], chat_id, message_id=mid)

    elif action == 'clean':
        plan_files = sorted(engine.STATE_DIR.glob('plan_*.json'),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        if not plan_files:
            _send(token, chat_id, '⚠️ 还没有扫描计划，请先发送 /check'); return
        plan_id = plan_files[0].stem.replace('plan_', '')
        _send(token, chat_id,
              f'🗑️ 将对最近计划 <code>{plan_id}</code> 执行清理。\n\n⚠️ 此操作不可撤销！',
              _clean_confirm_keyboard(plan_id))

    elif action in ('ingest', 'stats'):
        force = 'force' in (arg or '').lower()
        if force:
            _send(token, chat_id, '⏳ 正在强制刷新入库统计（现场扫 Emby）...')
        _spawn_and_watch('ingest', engine.ACTIONS['stats'], chat_id,
                         kw='force' if force else '')

    elif action == 'played':
        _spawn_and_watch('played', engine.ACTIONS['played'], chat_id)

    elif action == 'emby':
        _send(token, chat_id, '⏳ 正在拉取 Emby 库...')
        _spawn_and_watch('emby', engine.action_emby_library, chat_id)

    elif action == 'gap':
        _send(token, chat_id, '⏳ 正在检测缺集（首次可能较慢）...')
        _spawn_and_watch('gap', engine.action_emby_library, chat_id, with_tmdb=True)

    elif action == 'logs':
        _spawn_and_watch('logs', engine.ACTIONS['logs'], chat_id, kw='20')

    elif action == 'sub':
        if arg.lower() in ('check', 'now', '立即'):
            _send(token, chat_id, '⏳ 正在检查订阅（含 TMDB 对照）...')

            def _do_check():
                try:
                    r = engine.check_subscriptions(send_notify=False)
                    ups = r.get('updates') or []
                    if not ups:
                        _send(token, chat_id,
                              f"🔔 <b>订阅检查完成</b>\n共 {r.get('total', 0)} 部\n\n✅ 无新集、无缺集")
                    else:
                        lines = ['🔔 <b>订阅检查 · 变化</b>', '━━━━━━━━━━━━━━━━━━']
                        for u in ups:
                            lines.append(f"📺 《{u['name']}》")
                            if u.get('new_ep'):
                                lines.append(f"   🆕 {u['new_ep']['old']} → <b>{u['new_ep']['new']}</b>")
                            if u.get('missing'):
                                m = u['missing']
                                lines.append(f"   ⚠️ 缺 {m['diff']} 集（TMDB 已播 {m['tmdb_total']}）")
                        _send(token, chat_id, '\n'.join(lines))
                except Exception as e:
                    _send(token, chat_id, f'❌ 检查失败: {e}')
            threading.Thread(target=_do_check, daemon=True).start()
        else:
            subs = _cfg.get_subscriptions()
            state = engine._load_sub_state()
            if not subs:
                _send(token, chat_id,
                      '🔔 暂无订阅。\n在 Web「追更订阅」页添加，或从「影视探索」点订阅按钮。')
            else:
                lines = [f'🔔 <b>追更订阅</b>（{len(subs)} 部）', '━━━━━━━━━━━━━━━━━━']
                for s in subs[:20]:
                    sid = s.get('id') or s.get('tmdb_id') or s.get('name')
                    st = state.get(sid) or {}
                    cur_ep = st.get('latest_ep') or '—'
                    tmdb_total = st.get('tmdb_total') or 0
                    flag = '' if s.get('enabled', True) else ' (已暂停)'
                    extra = f" · TMDB {tmdb_total} 集" if tmdb_total else ''
                    lines.append(f"• 《{s['name']}》 <code>{cur_ep}</code>{extra}{flag}")
                if len(subs) > 20:
                    lines.append(f'… 共 {len(subs)} 部')
                _send(token, chat_id, '\n'.join(lines))

    elif action == 'morning':
        _send(token, chat_id, '☀️ 正在生成并发送晨报（立即现场扫描）...')

        def _do_morning():
            try:
                mr = _cfg.get_morning_report()
                ok = engine.send_morning_report(
                    mr.get('items') or ['stats', 'subscriptions', 'emby_gap'],
                    force_refresh=True)
                if not ok:
                    _send(token, chat_id, '❌ 晨报发送失败，检查 Telegram 配置')
            except Exception as e:
                _send(token, chat_id, f'❌ 晨报失败: {e}')
        threading.Thread(target=_do_morning, daemon=True).start()

    elif action == 'search_prompt':
        _send(token, chat_id,
              '🔍 请输入要搜索的片名：\n\n用法：<code>/search 关键词</code>',
              ttl=20, delete_user_msg=user_msg_id)

    elif action.startswith('search:'):
        q = action[7:]
        _spawn_and_watch('search', engine.ACTIONS['search'], chat_id, kw=q)


# ═══════════════════ 结果渲染 ═══════════════════
def _notify_result(token, chat_id, kind, res, message_id=None, user_msg_id=None):
    if not isinstance(res, dict):
        _send(token, chat_id, f'⚠️ {kind} 返回异常'); return
    if res.get('status') == 'error':
        _send(token, chat_id, f'❌ {kind} 失败: {res.get("message", "未知错误")}'); return

    if kind == 'check':
        pid = res.get('plan_id')
        total = res.get('del_local_cnt', 0) + res.get('del_share_cnt', 0)
        text = (f'🔍 <b>扫描完成</b>\n'
                f'━━━━━━━━━━━━━━━━━━\n'
                f'待清理本地: <b>{res.get("del_local_cnt", 0)}</b> 项\n'
                f'待淘汰分享: <b>{res.get("del_share_cnt", 0)}</b> 项\n'
                f'受保护: <b>{len(res.get("protected_items", []))}</b> 项\n'
                f'白名单豁免: <b>{len(res.get("exempted_items", []))}</b> 项\n')
        if total == 0:
            text += '\n✅ 双库状态良好，无需清理'
            kb = {'inline_keyboard': []}
        else:
            kb = _plan_keyboard(pid) if pid else {'inline_keyboard': []}
        if message_id: _edit(token, chat_id, message_id, text, kb)
        else: _send(token, chat_id, text, kb)

    elif kind == 'clean':
        text = (f'🗑️ <b>清理完成</b>\n━━━━━━━━━━━━━━━━━━\n'
                f'释放本地: <b>{res.get("loc_cnt", 0)}</b> 项\n'
                f'淘汰分享: <b>{res.get("sh_cnt", 0)}</b> 项\n'
                f'Emby 刷新: {"✅" if res.get("refreshed") else "❌"}\n')
        if message_id: _edit(token, chat_id, message_id, text, {'inline_keyboard': []})
        else: _send(token, chat_id, text)

    elif kind == 'search':
        text = (res.get('text') or '无结果').replace('**', '').replace('`', '')
        _send(token, chat_id, f'🔍 <b>搜索结果</b>\n\n{text}')

    elif kind == 'ingest':
        st = res.get('stats') or {}
        cache_ts = res.get('cache_ts') or 0
        from_cache = res.get('from_cache')
        age = int(time.time() - cache_ts) if cache_ts else None
        if age is None: age_str = '—'
        elif age < 60: age_str = f'{age} 秒前'
        elif age < 3600: age_str = f'{age // 60} 分钟前'
        else: age_str = f'{age // 3600} 小时前'
        text = (f'📊 <b>24H 入库</b>\n'
                f'━━━━━━━━━━━━━━━━━━\n'
                f'🎬 电影 +<b>{st.get("movies", 0)}</b> 部\n'
                f'📺 剧集 +<b>{st.get("series", 0)}</b> 部 / +<b>{st.get("episodes", 0)}</b> 集\n\n'
                f'<i>{"📦 来自缓存" if from_cache else "🔄 现场扫描"} · {age_str}</i>')
        _send(token, chat_id, text)

    elif kind == 'played':
        alerts = res.get('alerts') or []
        records = res.get('records') or []
        parts = []
        if alerts:
            parts.append('<b>🔔 追更提醒</b>')
            for a in alerts[:5]:
                parts.append('• ' + a.get('text', '').replace('**', '').replace('`', ''))
        if records:
            parts.append('')
            parts.append('<b>👤 正在观看</b>')
            for r in records[:8]:
                parts.append('• ' + r.get('text', '').replace('**', '').replace('`', ''))
        text = '\n'.join(parts) if parts else '✅ 暂无播放记录'
        _send(token, chat_id, f'▶️ <b>24H 播放记录</b>\n\n{text}')

    elif kind == 'logs':
        text = res.get('text') or '暂无日志'
        if not text.strip(): text = '暂无日志'
        _send(token, chat_id, f'📜 <b>审计日志</b>\n\n<pre>{text[:3500]}</pre>')

    elif kind == 'emby':
        st = res.get('stats') or {}
        text = (f'📺 <b>Emby 库总览</b>\n━━━━━━━━━━━━━━━━━━\n'
                f'剧集总数: <b>{st.get("total_series", 0)}</b>\n'
                f'完整: <b>{st.get("complete_series", 0)}</b>\n'
                f'缺集: <b>{st.get("incomplete_series", 0)}</b>\n'
                f'电影总数: <b>{st.get("total_movies", 0)}</b>\n')
        broken = [s for s in (res.get('series') or []) if not s.get('complete')]
        if broken:
            text += '\n<b>缺集 TOP 5:</b>\n'
            for s in broken[:5]:
                text += f'• 《{s.get("name")}》缺 {s.get("missing_eps")} 集\n'
        _send(token, chat_id, text)

    elif kind == 'gap':
        st = res.get('stats') or {}
        series = res.get('series') or []
        broken = sorted([s for s in series if not s.get('complete')],
                        key=lambda x: -(x.get('missing_eps') or 0))
        text = (f'🧩 <b>缺集检测</b>\n━━━━━━━━━━━━━━━━━━\n'
                f'剧集总数: <b>{st.get("total_series", 0)}</b>\n'
                f'完整: <b>{st.get("complete_series", 0)}</b>\n'
                f'缺集: <b>{st.get("incomplete_series", 0)}</b>\n')
        if broken:
            text += '\n<b>缺集 TOP 10:</b>\n'
            for s in broken[:10]:
                text += f'• 《{s.get("name")}》缺 <b>{s.get("missing_eps")}</b> 集\n'
        _send(token, chat_id, text)


# ═══════════════════ 消息处理 ═══════════════════
def _handle_command(token, msg):
    chat_id = msg['chat']['id']
    user_id = msg['from']['id']
    text = (msg.get('text') or '').strip()
    msg_id = msg.get('message_id')

    if not _is_allowed(user_id, chat_id):
        _send(token, chat_id,
              f'⛔ 你没有权限使用此 Bot。\n你的 ID: <code>{user_id}</code>')
        return

    if not text.startswith('/'):
        _send(token, chat_id,
              '请输入命令，或点输入框左侧 ☰ 图标。\n发 /help 查看帮助。',
              delete_user_msg=msg_id)
        return

    parts = text.split()
    cmd = parts[0].lower().split('@')[0]
    arg = ' '.join(parts[1:])

    if cmd in ('/start', '/menu'):   _dispatch('menu', token, chat_id, user_msg_id=msg_id)
    elif cmd == '/reset':            _reset_menu(token, chat_id, user_msg_id=msg_id)
    elif cmd == '/help':             _dispatch('help', token, chat_id, user_msg_id=msg_id)
    elif cmd == '/status':           _dispatch('status', token, chat_id, user_msg_id=msg_id)
    elif cmd == '/check':            _dispatch('check', token, chat_id, user_msg_id=msg_id)
    elif cmd == '/clean':            _dispatch('clean', token, chat_id, user_msg_id=msg_id)
    elif cmd == '/ingest':           _dispatch('ingest', token, chat_id, user_msg_id=msg_id, arg=arg)
    elif cmd == '/stats':            _dispatch('stats', token, chat_id, user_msg_id=msg_id, arg=arg)
    elif cmd == '/played':           _dispatch('played', token, chat_id, user_msg_id=msg_id)
    elif cmd == '/emby':             _dispatch('emby', token, chat_id, user_msg_id=msg_id)
    elif cmd == '/gap':              _dispatch('gap', token, chat_id, user_msg_id=msg_id)
    elif cmd == '/sub':              _dispatch('sub', token, chat_id, user_msg_id=msg_id, arg=arg)
    elif cmd == '/morning':          _dispatch('morning', token, chat_id, user_msg_id=msg_id)
    elif cmd == '/logs':             _dispatch('logs', token, chat_id, user_msg_id=msg_id)
    elif cmd == '/search':
        if not arg:
            _send(token, chat_id, '用法: <code>/search 关键词</code>',
                  delete_user_msg=msg_id)
            return
        _dispatch('search:' + arg, token, chat_id, user_msg_id=msg_id)
    else:
        _send(token, chat_id, f'❓ 未知命令: {cmd}\n发送 /help 查看帮助',
              delete_user_msg=msg_id)


def _handle_callback(token, cb):
    chat_id = cb['message']['chat']['id']
    user_id = cb['from']['id']
    message_id = cb['message']['message_id']
    data = cb.get('data') or ''

    if not _is_allowed(user_id, chat_id):
        _answer_callback(token, cb['id'], '⛔ 无权限', alert=True); return
    _answer_callback(token, cb['id'])

    if data.startswith('cmd:'):
        _dispatch(data[4:], token, chat_id, message_id=message_id)

    elif data.startswith('plan:'):
        plan_id = data[5:]
        safe_id = re.sub(r'[^0-9a-f]', '', plan_id)
        pf = engine.STATE_DIR / f'plan_{safe_id}.json'
        if not pf.exists():
            _answer_callback(token, cb['id'], '计划已失效', alert=True); return
        try:
            plan = json.loads(pf.read_text(encoding='utf-8'))
            keys = plan.get('keys', [])
        except Exception:
            _send(token, chat_id, '⚠️ 计划文件损坏'); return
        preview = '\n'.join(f'• {k.split("|", 1)[1]}' for k in keys[:20])
        more = f'\n… 共 {len(keys)} 条' if len(keys) > 20 else ''
        _send(token, chat_id, f'📋 <b>计划清单</b> ({len(keys)} 项)\n\n{preview}{more}')

    elif data.startswith('clean_ask:'):
        plan_id = data[10:]
        _edit(token, chat_id, message_id,
              f'⚠️ <b>确认执行清理？</b>\n\n计划 ID: <code>{plan_id}</code>\n'
              f'将按清单删除本地 STRM（含 115 云端源）和分享 STRM。\n\n此操作不可撤销！',
              _clean_confirm_keyboard(plan_id))

    elif data.startswith('clean_do:'):
        plan_id = data[9:]
        _edit(token, chat_id, message_id, '⏳ 正在执行清理...', {'inline_keyboard': []})
        _spawn_and_watch('clean', engine.ACTIONS['inter_clean'], chat_id,
                         message_id=message_id, plan=plan_id, dry_run=False)

    elif data == 'dismiss':
        _try_delete(token, chat_id, message_id)


# ═══════════════════ 长轮询 ═══════════════════
def _poll_loop():
    log.info('Bot 长轮询线程启动')
    while _state['running']:
        try:
            cfg = load_config()
            token = (cfg.get('telegram_bot_token') or '').strip()
            if not token:
                time.sleep(30); continue

            if not _state['initialized']:
                try:
                    me = _api(token, 'getMe', {}, timeout=10)
                    if me.get('ok'):
                        _state['bot_username'] = me['result'].get('username', '')
                        log.info('Bot 已连接: @%s', _state['bot_username'])
                        _init_bot(token)
                        _state['initialized'] = True
                except Exception as e:
                    log.warning('getMe 失败: %s', e)

            try:
                resp = _api(token, 'getUpdates', {
                    'offset': _state['offset'], 'timeout': 25,
                    'allowed_updates': json.dumps(['message', 'callback_query']),
                }, timeout=35)
            except Exception as e:
                _state['last_error'] = str(e)
                log.warning('getUpdates 失败: %s', e)
                time.sleep(5); continue

            _state['last_poll'] = time.time()
            if not resp.get('ok'):
                time.sleep(3); continue

            for upd in resp.get('result', []):
                _state['offset'] = upd['update_id'] + 1
                try:
                    if 'message' in upd: _handle_command(token, upd['message'])
                    elif 'callback_query' in upd: _handle_callback(token, upd['callback_query'])
                except Exception as e:
                    log.warning('处理 update 失败: %s', e)

        except Exception as e:
            _state['last_error'] = str(e)
            log.warning('Bot 循环异常: %s', e)
            time.sleep(5)

    log.info('Bot 长轮询线程退出')


def start():
    if _state['running']: return
    token = (load_config().get('telegram_bot_token') or '').strip()
    if not token:
        log.info('Telegram Bot Token 未配置，跳过启动'); return
    _state['running'] = True
    t = threading.Thread(target=_poll_loop, daemon=True, name='tg-bot')
    t.start()
    _state['thread'] = t
    c = threading.Thread(target=_cleanup_loop, daemon=True, name='tg-cleanup')
    c.start()
    log.info('Telegram Bot 已启动')


def stop():
    _state['running'] = False


def restart():
    stop()
    # 重置连接状态，让 _poll_loop 重新 getMe + setMyCommands，
    # 否则改了 Bot Token 后用户名/命令列表会停留在旧值。
    _state['initialized'] = False
    _state['bot_username'] = ''
    _state['last_error'] = ''
    # offset 保留：避免重启后重复处理旧消息
    time.sleep(0.5)
    start()


def status():
    with _DELETE_LOCK:
        pending = len(_delete_queue)
    return {
        'running': _state['running'],
        'bot_username': _state['bot_username'],
        'last_poll_ago': int(time.time() - _state['last_poll']) if _state['last_poll'] else None,
        'last_error': _state['last_error'],
        'pending_deletes': pending,
    }