# -*- coding: utf-8 -*-
"""Telegram Bot —— 长轮询模式
特性：
  - 无底部常驻键盘，界面极简，靠 ☰ 命令菜单操作
  - 操作反馈卡片 60 秒自动销毁（阅后即焚）
  - 用户命令消息 10 秒自动删除（所有命令统一在入口登记，不再依赖各分支单独传参）
  - 计划卡片到期自动消失；点到过期/已用/失效的卡片会就地提示并给「重新扫描」
  - 待删队列落盘，重启后不丢
  - 欢迎提示 15 秒即焚，只保留最新一条

命令/动作/渲染都用表驱动（COMMANDS / _ACTIONS / _CALLBACKS / _RENDER），
不再靠一长串 if/elif；HTML 拼装与发送统一走 app/tgmsg。
"""
import json, time, logging, threading
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor

from . import config as _cfg
from . import state, governance, ingest, morning, storage, subscribe, tasks
from .tgmsg import html as th, reports as tgmsg_reports, transport as tgmsg_transport

# 测试替身挂载点：tests 直接替换 bot.load_config，所以这里保留模块级名字
load_config = _cfg.load_config

log = logging.getLogger('media_agent.bot')

_state = {
    'running': False, 'thread': None, 'offset': 0,
    'last_error': '', 'last_poll': 0, 'bot_username': '',
    'initialized': False,
    'fail_count': 0,        # getUpdates 连续失败次数，成功一次清零
    'last_error_ts': 0,
    'queue_loaded': False,
    'gen': 0,  # 每次 start() +1；旧代的轮询线程发现代数变了就退出
    'token': '',  # 缓存的 Bot Token，start/restart 时刷新，清理线程不再每 2 秒 load_config
}
_LOCK = threading.Lock()

_delete_queue = []
_DELETE_LOCK = threading.Lock()
BOT_MSG_TTL  = 60
USER_MSG_TTL = 10
MENU_MSG_TTL = 15

_last_menu_msg = {}
_MENU_LOCK = threading.Lock()

# 更新处理线程池：4 个固定工作线程 + 有界待处理队列（满了就丢弃并告警），
# 替代「每个 update 起一个线程」——几十条消息同时进来时不再瞬间起几十个线程。
MAX_PENDING_UPDATES = 32
_update_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='tg-update')
_pending_updates = 0
_POOL_LOCK = threading.Lock()


# ═══════════════════ 基础 ═══════════════════
def _get_current_task():
    return tasks.manager.running()


def _token():
    return (_state.get('token') or '').strip()


# ═══════════════════ 阅后即焚队列 ═══════════════════
_delete_fail = {}          # (chat_id, mid) -> 已重试次数
_DELETE_RETRY = 3
_DELETE_RETRY_DELAY = 20


def _persist_queue_locked():
    """持久化待删队列（SQLite 文档 bot_delete_queue；调用方须已持有 _DELETE_LOCK）。以前队列只在内存，
    重启一次，所有卡片 / 命令消息的删除计划全部丢失，消息就永远留在聊天里。"""
    storage.db_doc_put(storage.DOC_BOT_DELETE_QUEUE, [list(x) for x in _delete_queue])


def _load_queue():
    data = storage.db_doc_get(storage.DOC_BOT_DELETE_QUEUE)
    if not isinstance(data, list):
        return
    with _DELETE_LOCK:
        known = {m for (_, _, m) in _delete_queue}
        for item in data:
            try:
                d, c, m = item
                d = float(d)
            except (TypeError, ValueError):
                continue
            if m not in known:
                _delete_queue.append((d, c, m))


def _schedule_delete(chat_id, message_id, delay):
    if not message_id: return
    with _DELETE_LOCK:
        _delete_queue[:] = [(d, c, m) for (d, c, m) in _delete_queue if m != message_id]
        _delete_queue.append((time.time() + delay, chat_id, message_id))
        _persist_queue_locked()


def _try_delete(token, chat_id, message_id):
    try:
        tgmsg_transport.delete(token, chat_id, message_id, timeout=10)
        return True
    except Exception as e:
        desc = tgmsg_transport.err_desc(e)
        if 'not found' in desc.lower():
            return True   # 已经不在了，等同删除成功
        log.debug('删除消息失败 (%s/%s): %s', chat_id, message_id, desc)
        return False


def _alive(gen):
    return _state['running'] and _state['gen'] == gen


def _cleanup_loop(gen):
    log.info('阅后即焚线程启动')
    while _alive(gen):
        time.sleep(2)
        token = _token()  # 缓存的 token，不再每次 load_config（配置变更走 restart 刷新）
        if not token: continue
        now = time.time()
        # 只在锁内取出到期项；deleteMessage 是网络调用（最长 10 秒），
        # 以前整个循环都握着锁，网络一抖，_send / _edit（轮询线程）和 Web 的 status() 全被卡住。
        with _DELETE_LOCK:
            due = [x for x in _delete_queue if now >= x[0]]
            if not due: continue
            _delete_queue[:] = [x for x in _delete_queue if now < x[0]]
            _persist_queue_locked()
        for _deadline, chat_id, mid in due:
            if _try_delete(token, chat_id, mid):
                _delete_fail.pop((chat_id, mid), None)
                continue
            n = _delete_fail.get((chat_id, mid), 0) + 1
            if n >= _DELETE_RETRY:
                _delete_fail.pop((chat_id, mid), None)
                continue
            _delete_fail[(chat_id, mid)] = n
            with _DELETE_LOCK:   # 期间若被重新登记（例如卡片又被编辑）就不要覆盖新的计划
                if not any(m == mid for (_, _, m) in _delete_queue):
                    _delete_queue.append((time.time() + _DELETE_RETRY_DELAY, chat_id, mid))
                    _persist_queue_locked()


# ═══════════════════ 发送 / 编辑 ═══════════════════
def _send(token, chat_id, text, keyboard=None, reply_to=None,
          ttl=None, delete_user_msg=None):
    result = tgmsg_transport.send(token, chat_id, text, keyboard=keyboard,
                                  reply_to=reply_to, timeout=15)
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
    tgmsg_transport.answer_cb(token, cb_id, text=text, alert=alert, timeout=10)


def _edit(token, chat_id, message_id, text, keyboard=None, ttl=None):
    result = tgmsg_transport.edit(token, chat_id, message_id, text,
                                  keyboard=keyboard, timeout=15)
    if result and result.get('ok'):
        delay = BOT_MSG_TTL if ttl is None else ttl
        if delay > 0:
            _schedule_delete(chat_id, message_id, delay)
    return result


def _edit_or_send(token, chat_id, message_id, text, keyboard=None, ttl=None):
    """优先就地编辑卡片；卡片已被删 / 编辑失败时退回发新消息，保证用户总能看到结果。"""
    if message_id:
        r = _edit(token, chat_id, message_id, text, keyboard, ttl=ttl)
        if r and r.get('ok'):
            return r
    return _send(token, chat_id, text, keyboard, ttl=ttl)


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


def _close_keyboard():
    return {'inline_keyboard': [[{'text': '❌ 关闭', 'callback_data': 'dismiss'}]]}


def _rescan_keyboard():
    return {'inline_keyboard': [[
        {'text': '🔄 重新扫描', 'callback_data': 'cmd:check'},
        {'text': '❌ 关闭', 'callback_data': 'dismiss'}]]}


def _plan_ttl_left(plan_id, default=None):
    """计划剩余有效秒数：计划卡片据此设置自动销毁，到期即消失。"""
    plan = governance.load_plan(plan_id) if plan_id else None
    if not plan:
        return default
    left = float(plan.get('ts') or 0) + state.PLAN_TTL - time.time()
    return max(int(left), 10)


def _plan_problem(plan_id):
    """校验计划还能不能执行。返回 (code, 提示文案)，可执行则 (None, '')。
    顺手把已超时但仍是 pending 的计划落盘标成 expired。"""
    plan = governance.load_plan(plan_id)
    if plan is None:
        return 'missing', '计划已失效或不存在'
    st = plan.get('state')
    if st in ('done', 'failed', 'stale'):
        return 'used', '计划已执行过或已失效，不能重复执行'
    if st == 'executing' and _get_current_task() is not None:
        return 'executing', '计划正在执行中，请稍候'
    if st == 'expired' or time.time() - float(plan.get('ts') or 0) > state.PLAN_TTL:
        if st == 'pending':
            governance.save_plan_state(plan_id, 'expired')
        return 'expired', f'计划已超过 {state.PLAN_TTL // 3600} 小时有效期，已过期'
    sig = str(plan.get('rule_sig') or '')
    try:
        cur_sig = governance.current_rule_snapshot()['sig']
    except Exception as e:
        log.debug('读取规则快照失败: %s', e)
        cur_sig = ''
    if sig and cur_sig and sig != cur_sig:
        return 'rule_changed', '清理规则在扫描后已变化，旧计划不再适用'
    return None, ''


def _show_plan_problem(token, chat_id, message_id, code, text):
    """点到过期 / 已用 / 失效的计划卡片：就地改成提示，并给「重新扫描 / 关闭」。"""
    if code == 'executing':
        _edit(token, chat_id, message_id, f'⏳ {th.h(text)}', _close_keyboard(), ttl=20)
    else:
        _edit_or_send(token, chat_id, message_id,
                      f'⌛ <b>{th.h(text)}</b>\n\n请重新扫描生成新计划。',
                      _rescan_keyboard(), ttl=60)


def _dismiss(token, chat_id, message_id):
    """忽略 / 取消：删除卡片；删不掉（超 48 小时、网络抖动）就把卡片改成已忽略并去掉按钮。"""
    if _try_delete(token, chat_id, message_id):
        return
    _edit(token, chat_id, message_id, '✅ 已忽略', {'inline_keyboard': []}, ttl=5)


# ═══════════════════ 命令注册 ═══════════════════
# 命令表：一张表同时生成 Telegram 命令菜单（MY_COMMANDS）与 /help 文案。
#   action   : 分发表 _ACTIONS 的键
#   section  : help 分组标题，None 表示不进 help
#   help     : help 里的一行；None 表示不显示
#   arg      : 命令带参数
#   special  : 'search' 需要把关键词拼进 action
COMMANDS = [
    {'command': 'check',   'description': '⚖️ 扫描双库',                'action': 'check',   'section': '治理', 'help': '/check　扫描双库'},
    {'command': 'clean',   'description': '🗑️ 执行清理',                'action': 'clean',   'section': '治理', 'help': '/clean　清理最近一次计划'},
    {'command': 'ingest',  'description': '📊 入库速报（/ingest force 强制刷新）', 'action': 'ingest', 'section': '统计', 'help': '/ingest　24 小时入库（/ingest force 强制刷新）', 'arg': True},
    {'command': 'stats',   'description': '📊 24H 入库（同 ingest）',   'action': 'stats',   'section': None,   'help': None, 'arg': True},
    {'command': 'played',  'description': '▶️ 24H 播放记录',            'action': 'played',  'section': '统计', 'help': '/played　24 小时播放'},
    {'command': 'emby',    'description': '📺 Emby 库总览',             'action': 'emby',    'section': '统计', 'help': '/emby　Emby 库总览'},
    {'command': 'gap',     'description': '🧩 缺集检测',                'action': 'gap',     'section': '统计', 'help': '/gap　缺集检测'},
    {'command': 'search',  'description': '🔍 搜索片名',                'action': 'search',  'section': '其它', 'help': '/search 关键词　搜片', 'arg': True, 'special': 'search'},
    {'command': 'sub',     'description': '🔔 追更订阅',                'action': 'sub',     'section': '订阅', 'help': '/sub　查看订阅（/sub check 立即检查）', 'arg': True},
    {'command': 'morning', 'description': '☀️ 立即发送晨报',            'action': 'morning', 'section': '订阅', 'help': '/morning　立即发送晨报'},
    {'command': 'logs',    'description': '📜 审计日志',                'action': 'logs',    'section': '其它', 'help': '/logs [n]　审计日志'},
    {'command': 'menu',    'description': '🎛️ 显示菜单',                'action': 'menu',    'section': '其它', 'help': '/menu　/reset　菜单'},
    {'command': 'status',  'description': '📡 运行状态',                'action': 'status',  'section': '其它', 'help': '/status　运行状态'},
    {'command': 'reset',   'description': '🔄 重置面板',                'action': 'reset',   'section': None,   'help': None},
    {'command': 'help',    'description': '⚙️ 帮助',                    'action': 'help',    'section': '其它', 'help': '/help　帮助'},
]

MY_COMMANDS = [{'command': c['command'], 'description': c['description']} for c in COMMANDS]

_SECTION_TITLES = [('治理', '🧹 <b>治理</b>'), ('统计', '📊 <b>统计</b>'),
                   ('订阅', '🔔 <b>订阅 · 晨报</b>'), ('其它', '🔧 <b>其它</b>')]


def _build_help_text():
    lines = ['🎬 <b>TTD Guard</b>',
             '<i>点输入框左侧 ☰ 选功能，或直接输命令</i>']
    for key, header in _SECTION_TITLES:
        rows = [c['help'] for c in COMMANDS if c.get('section') == key and c.get('help')]
        if rows:
            lines += ['', header] + rows
    lines += ['', '<i>💡 反馈卡片 60 秒后自动销毁</i>']
    return '\n'.join(lines)


HELP_TEXT = _build_help_text()


def _init_bot(token):
    try:
        tgmsg_transport.api(token, 'setMyCommands', {
            'commands': json.dumps(MY_COMMANDS, ensure_ascii=False),
            'scope': json.dumps({'type': 'default'}),
        }, timeout=10)
        log.info('Bot 命令列表已注册')
    except Exception as e:
        log.warning('setMyCommands 失败: %s', e)


# ═══════════════════ 任务派生 ═══════════════════
LIST_MSG_TTL = 600  # 带折叠清单的消息保留 10 分钟，够展开/收起慢慢看


def _send_long(token, chat_id, text, ttl=None):
    """超长消息自动切段；切在折叠引用块里时每段都补全 blockquote 标签。"""
    for part in th.split_telegram_html(text):
        _send(token, chat_id, part, ttl=ttl)


def _busy_msg():
    cur = _get_current_task()
    if cur is not None and cur.status == 'running':
        return f'⏳ 已有任务 [{cur.kind}] 正在执行，请稍候'
    return ''


# 这些 Bot 动作会遍历双库或改文件，必须走统一任务总线与 Web / 定时巡检互斥
_EXCLUSIVE_KINDS = {'check': 'inter_check', 'clean': 'inter_clean'}


def _spawn_and_watch(kind, action_fn, chat_id, message_id=None,
                     user_msg_id=None, **kwargs):
    token = _token() or (load_config().get('telegram_bot_token') or '').strip()
    if not token: return
    # 先放默认值再用调用方参数覆盖。以前写成 Namespace(kw='', ..., **kwargs)，
    # 只要调用方也传 kw / plan / dry_run（搜片、/logs、/ingest、清理确认），
    # 就会抛 "multiple values for keyword argument"，而且是在线程启动前抛出，
    # 用户看不到任何回复。
    _defaults = dict(kw='', plan='', dry_run=False)
    _defaults.update(kwargs)
    args = Namespace(**_defaults)

    def on_done(t):
        if isinstance(t.result, dict):
            _notify_result(token, chat_id, kind, t.result, message_id, user_msg_id)
        else:
            # 任务抛异常：以前只另发一条消息，原来那张「⏳ 正在执行…」卡片永远停在那里
            _show_failure(token, chat_id, kind,
                          {'status': 'error', 'message': t.error or '未知错误'}, message_id)

    exclusive = kind in _EXCLUSIVE_KINDS
    try:
        tasks.manager.spawn(_EXCLUSIVE_KINDS.get(kind, 'bot_' + kind), action_fn, args,
                            exclusive=exclusive, source='bot', on_done=on_done)
    except tasks.TaskBusy as e:
        txt = f'⏳ {e}'
        if message_id: _edit(token, chat_id, message_id, txt, _close_keyboard(), ttl=20)
        else: _send(token, chat_id, txt)


def _latest_pending_plan_id():
    """最近一份仍可执行（pending 且未过期）的计划：按计划生成时间 ts 取最新，不看更新时间。"""
    return storage.db_latest_pending_plan(state.PLAN_TTL)


# ═══════════════════ 菜单 ═══════════════════
def _show_menu(token, chat_id, user_msg_id=None):
    with _MENU_LOCK:
        old = _last_menu_msg.get(chat_id)
    if old:
        _try_delete(token, chat_id, old)
    m = _send(token, chat_id,
              '🎬 <b>TTD Guard</b>\n\n点击输入框左侧 <b>☰</b>，或输入 / 查看命令',
              _clear_keyboard(),
              ttl=MENU_MSG_TTL,
              delete_user_msg=user_msg_id)
    if m and m.get('ok'):
        with _MENU_LOCK:
            _last_menu_msg[chat_id] = m.get('result', {}).get('message_id')


def _reset_menu(token, chat_id, user_msg_id=None):
    try:
        tgmsg_transport.api(token, 'sendMessage', {
            'chat_id': str(chat_id),
            'text': '🔄 面板已重置',
            'reply_markup': json.dumps({'remove_keyboard': True}),
        }, timeout=10)
    except Exception as e:
        log.warning('清空键盘失败: %s', e)
    _show_menu(token, chat_id, user_msg_id=user_msg_id)


# ═══════════════════ 动作处理（_ACTIONS 分发表）═══════════════════
def _act_menu(token, chat_id, message_id=None, user_msg_id=None, arg=''):
    _show_menu(token, chat_id, user_msg_id=user_msg_id)


def _act_help(token, chat_id, message_id=None, user_msg_id=None, arg=''):
    if message_id: _edit(token, chat_id, message_id, HELP_TEXT)
    else: _send(token, chat_id, HELP_TEXT)


def _act_status(token, chat_id, message_id=None, user_msg_id=None, arg=''):
    cur = _get_current_task()
    s = (f'⏳ 运行中: <b>{th.h(cur.kind)}</b>' if (cur and cur.status == 'running')
         else '✅ 空闲中')
    if message_id: _edit(token, chat_id, message_id, s)
    else: _send(token, chat_id, s)


def _act_check(token, chat_id, message_id=None, user_msg_id=None, arg=''):
    bm = _busy_msg()
    if bm:
        _send(token, chat_id, bm); return
    if message_id:
        _edit(token, chat_id, message_id, '⏳ 正在扫描双库...', {'inline_keyboard': []})
        _spawn_and_watch('check', governance.action_inter_check, chat_id, message_id=message_id)
    else:
        m = _send(token, chat_id, '⏳ 正在扫描双库...')
        mid = (m or {}).get('result', {}).get('message_id')
        _spawn_and_watch('check', governance.action_inter_check, chat_id, message_id=mid)


def _act_clean(token, chat_id, message_id=None, user_msg_id=None, arg=''):
    plan_id = _latest_pending_plan_id()
    if not plan_id:
        _send(token, chat_id, '⚠️ 没有可执行的扫描计划（未扫描或已过期/已执行），请先发送 /check'); return
    _send(token, chat_id,
          f'🗑️ 将对最近计划 <code>{th.h(plan_id)}</code> 执行清理。\n\n⚠️ 此操作不可撤销！',
          _clean_confirm_keyboard(plan_id))


def _act_ingest(token, chat_id, message_id=None, user_msg_id=None, arg=''):
    force = 'force' in (arg or '').lower()
    if force:
        _send(token, chat_id, '⏳ 正在强制刷新入库统计（现场扫 Emby）...')
    _spawn_and_watch('ingest', ingest.action_stats, chat_id,
                     kw='full force' if force else 'full')


def _act_played(token, chat_id, message_id=None, user_msg_id=None, arg=''):
    _spawn_and_watch('played', ingest.action_played, chat_id)


def _job_gap(_args):
    """Emby 库总览 / 缺集检测共用同一份 gap_report（一次任务只算一次）。"""
    return morning.gap_report()


def _act_emby(token, chat_id, message_id=None, user_msg_id=None, arg=''):
    _send(token, chat_id, '⏳ 正在拉取 Emby 库...')
    _spawn_and_watch('emby', _job_gap, chat_id)


def _act_gap(token, chat_id, message_id=None, user_msg_id=None, arg=''):
    _send(token, chat_id, '⏳ 正在检测缺集（首次可能较慢）...')
    _spawn_and_watch('gap', _job_gap, chat_id)


def _act_logs(token, chat_id, message_id=None, user_msg_id=None, arg=''):
    _spawn_and_watch('logs', ingest.action_logs, chat_id, kw='20')


def _act_sub(token, chat_id, message_id=None, user_msg_id=None, arg=''):
    if (arg or '').lower() in ('check', 'now', '立即'):
        _send(token, chat_id, '⏳ 正在检查订阅（含 TMDB 对照）...')
        _spawn_and_watch('sub_check',
                         lambda _a: subscribe.check_subscriptions(send_notify=False), chat_id)
    else:
        subs = subscribe.get_subscriptions()
        sub_state = subscribe.load_sub_state()
        _send(token, chat_id, str(tgmsg_reports.sub_list(subs, sub_state)))


def _act_morning(token, chat_id, message_id=None, user_msg_id=None, arg=''):
    _send(token, chat_id, '☀️ 正在读取晨报缓存并发送（不现场扫描）...')

    def _job(_args):
        mr = _cfg.get_morning_report()
        # force_refresh=False 与卡片文案「不现场扫描」一致；mark_sent=False 避免手动发送
        # 把当天定时的每日晨报标记成已发送。
        ok = morning.send_morning_report(mr.get('items') or ['stats', 'subscriptions', 'emby_gap'],
                                         force_refresh=False, mark_sent=False)
        return {'status': 'success'} if ok else {
            'status': 'error', 'message': '晨报发送失败，检查 Telegram 配置'}

    _spawn_and_watch('morning', _job, chat_id)


def _act_search_prompt(token, chat_id, message_id=None, user_msg_id=None, arg=''):
    _send(token, chat_id,
          '🔍 请输入要搜索的片名：\n\n用法：<code>/search 关键词</code>',
          ttl=20, delete_user_msg=user_msg_id)


def _act_reset(token, chat_id, message_id=None, user_msg_id=None, arg=''):
    _reset_menu(token, chat_id, user_msg_id=user_msg_id)


_ACTIONS = {
    'menu': _act_menu, 'help': _act_help, 'status': _act_status, 'check': _act_check,
    'clean': _act_clean, 'ingest': _act_ingest, 'stats': _act_ingest, 'played': _act_played,
    'emby': _act_emby, 'gap': _act_gap, 'logs': _act_logs, 'sub': _act_sub,
    'morning': _act_morning, 'search_prompt': _act_search_prompt, 'reset': _act_reset,
}
_CMD_SPECS = {'/' + c['command']: c for c in COMMANDS}
_CMD_SPECS['/start'] = next(c for c in COMMANDS if c['command'] == 'menu')


def _dispatch(action, token, chat_id, message_id=None, user_msg_id=None, arg=''):
    if action.startswith('search:'):
        _spawn_and_watch('search', ingest.action_search, chat_id, kw=action[7:])
        return
    fn = _ACTIONS.get(action)
    if fn is None:
        log.warning('未知 Bot 动作: %s', action)
        return
    fn(token, chat_id, message_id=message_id, user_msg_id=user_msg_id, arg=arg)


# ═══════════════════ 结果渲染（_RENDER 分发表）═══════════════════
def _render_check(token, chat_id, res, message_id=None, user_msg_id=None):
    pid = res.get('plan_id')
    total = res.get('del_local_cnt', 0) + res.get('del_share_cnt', 0)
    text = str(tgmsg_reports.scan_card('🔍', '扫描完成', res))
    if total == 0:
        text += '\n\n✅ 双库状态良好，无需清理'
        kb = {'inline_keyboard': []}
    else:
        kb = _plan_keyboard(pid) if pid else {'inline_keyboard': []}
    # 带按钮的计划卡片保留到计划过期（到期自动消失），不再 60 秒就被销毁
    ttl = _plan_ttl_left(pid) if (pid and total) else None
    _edit_or_send(token, chat_id, message_id, text, kb, ttl=ttl)


def _render_clean(token, chat_id, res, message_id=None, user_msg_id=None):
    skipped = res.get('skipped') or 0  # 引擎返回的是跳过项数（int），以前按列表 len() 会在清理完成后抛错
    text = tgmsg_reports.clean_done(res.get('loc_cnt', 0), res.get('sh_cnt', 0),
                                    res.get('refreshed'), skipped)
    _edit_or_send(token, chat_id, message_id, str(text), {'inline_keyboard': []}, ttl=300)


def _render_search(token, chat_id, res, message_id=None, user_msg_id=None):
    items = res.get('items') or []
    total = res.get('total')
    if total is None:
        total = len(items)
    text = tgmsg_reports.search_result(items, total, res.get('kw') or '')
    _send(token, chat_id, f'🔍 <b>搜索结果</b>\n\n{text}')


def _render_ingest(token, chat_id, res, message_id=None, user_msg_id=None):
    _send_long(token, chat_id, str(tgmsg_reports.ingest_card(res)), ttl=LIST_MSG_TTL)


def _render_played(token, chat_id, res, message_id=None, user_msg_id=None):
    text = tgmsg_reports.played_card(res.get('records'), res.get('alerts'))
    _send(token, chat_id, str(text))


def _render_logs(token, chat_id, res, message_id=None, user_msg_id=None):
    _send(token, chat_id, str(tgmsg_reports.logs_card(res.get('text'))))


def _render_emby(token, chat_id, res, message_id=None, user_msg_id=None):
    _send_long(token, chat_id, str(tgmsg_reports.emby_overview(res)), ttl=LIST_MSG_TTL)


def _render_gap(token, chat_id, res, message_id=None, user_msg_id=None):
    _send_long(token, chat_id, str(tgmsg_reports.gap_card(res)), ttl=LIST_MSG_TTL)


def _render_sub_check(token, chat_id, res, message_id=None, user_msg_id=None):
    _send(token, chat_id, str(tgmsg_reports.sub_check(res.get('updates') or [],
                                                      res.get('total', 0))))


def _render_morning(token, chat_id, res, message_id=None, user_msg_id=None):
    """成功时晨报已由 send_morning_report 直接发出，这里不再补消息。"""
    return


_RENDER = {
    'check': _render_check, 'clean': _render_clean, 'search': _render_search,
    'ingest': _render_ingest, 'played': _render_played, 'logs': _render_logs,
    'emby': _render_emby, 'gap': _render_gap, 'sub_check': _render_sub_check,
    'morning': _render_morning,
}


def notify_auto_scan(res, error=None):
    """定时巡检结果推送（带「查看清单 / 执行清理」按钮）。返回是否发送成功。"""
    token = (state.RUNTIME_CFG.get('telegram_bot_token') or '').strip()
    chat_id = (state.RUNTIME_CFG.get('telegram_chat_id') or '').strip()
    if not token or not chat_id:
        return False
    if error:
        r = _send(token, chat_id, str(tgmsg_reports.auto_scan_error(error)))
        return bool(r and r.get('ok'))
    loc, shr = res.get('del_local_cnt', 0), res.get('del_share_cnt', 0)
    text = str(tgmsg_reports.auto_scan_card(res))
    pid = res.get('plan_id')
    kb = _plan_keyboard(pid) if (pid and loc + shr > 0) else {'inline_keyboard': []}
    # 带按钮的卡片跟计划同寿命：计划过期（PLAN_TTL）就自动消失，不再挂 12 小时变成点不动的死卡片
    ttl = _plan_ttl_left(pid, 43200) if (pid and loc + shr > 0) else 43200
    r = _send(token, chat_id, text, kb, ttl=ttl)
    return bool(r and r.get('ok'))


def _show_failure(token, chat_id, kind, res, message_id=None):
    """任务失败 / 忙：编辑原卡片而不是另发消息。
    以前 clean_do 先把卡片改成「⏳ 正在执行清理...」并去掉按钮，计划过期时引擎返回 error，
    Bot 却另发一条新消息，原卡片永远停在「执行中」，也没有任何按钮可点。"""
    text = str(tgmsg_reports.failure_card(kind, res))
    code = res.get('code')
    if res.get('status') == 'busy':
        kb = _close_keyboard()
    elif code in tgmsg_reports.RESCAN_CODES:
        kb = _rescan_keyboard()
    else:
        kb = _close_keyboard() if message_id else None
    _edit_or_send(token, chat_id, message_id, text, kb, ttl=60)


def _notify_result(token, chat_id, kind, res, message_id=None, user_msg_id=None):
    if not isinstance(res, dict):
        _send(token, chat_id, f'⚠️ {th.h(kind)} 返回异常'); return
    if res.get('status') in ('error', 'busy'):
        _show_failure(token, chat_id, kind, res, message_id); return
    fn = _RENDER.get(kind)
    if fn is None:
        log.warning('无渲染器: %s', kind); return
    fn(token, chat_id, res, message_id, user_msg_id)


# ═══════════════════ 消息处理 ═══════════════════
def _handle_command(token, msg):
    chat_id = msg['chat']['id']
    user_id = msg['from']['id']
    text = (msg.get('text') or '').strip()
    msg_id = msg.get('message_id')

    # 所有用户消息统一 10 秒后删除。以前只有 /menu、/search 空参数等少数分支
    # 把 delete_user_msg 传给了 _send，/check /search 关键词 /gap 等命令的原消息永远不删。
    if msg_id:
        _schedule_delete(chat_id, msg_id, USER_MSG_TTL)

    if not _is_allowed(user_id, chat_id):
        _send(token, chat_id,
              f'⛔ 你没有权限使用此 Bot。\n你的 ID: <code>{th.h(user_id)}</code>')
        return

    if not text.startswith('/'):
        _send(token, chat_id,
              '请输入命令，或点输入框左侧 ☰ 图标。\n发 /help 查看帮助。',
              delete_user_msg=msg_id)
        return

    parts = text.split()
    cmd = parts[0].lower().split('@')[0]
    arg = ' '.join(parts[1:])

    spec = _CMD_SPECS.get(cmd)
    if spec is None:
        _send(token, chat_id, str(tgmsg_reports.unknown_command(cmd)),
              delete_user_msg=msg_id)
        return
    if spec.get('special') == 'search':
        if not arg:
            _send(token, chat_id, '用法: <code>/search 关键词</code>',
                  delete_user_msg=msg_id)
            return
        _dispatch('search:' + arg, token, chat_id, user_msg_id=msg_id)
        return
    _dispatch(spec['action'], token, chat_id, user_msg_id=msg_id, arg=arg)


def _handle_callback(token, cb):
    msg = cb.get('message') or {}
    chat_id = (msg.get('chat') or {}).get('id')
    message_id = msg.get('message_id')
    user_id = (cb.get('from') or {}).get('id')
    data = cb.get('data') or ''

    if chat_id is None or message_id is None:
        _answer_callback(token, cb['id'], '卡片已失效，请重新发送命令', alert=True); return
    if not _is_allowed(user_id, chat_id):
        _answer_callback(token, cb['id'], '⛔ 无权限', alert=True); return
    _answer_callback(token, cb['id'])   # 一次回调只能应答一次，先应答让按钮不转圈；后续结果都落在卡片上

    for prefix, handler in _CALLBACKS:
        if data == prefix or (prefix.endswith(':') and data.startswith(prefix)):
            handler(token, chat_id, message_id, data); return
    log.debug('未识别的回调查询: %s', data)


def _cb_dismiss(token, chat_id, message_id, data):
    _dismiss(token, chat_id, message_id)


def _cb_cmd(token, chat_id, message_id, data):
    _dispatch(data[4:], token, chat_id, message_id=message_id)


def _cb_plan(token, chat_id, message_id, data):
    plan_id = data[5:]
    code, text = _plan_problem(plan_id)
    if code and code != 'executing':
        _show_plan_problem(token, chat_id, message_id, code, text); return
    plan_data = governance.load_plan(plan_id) or {}
    actions = plan_data.get('actions') or []
    if not actions:
        _send(token, chat_id, '📋 <b>计划清单</b>\n\n(空)'); return
    preview = '\n'.join('• ' + th.h(a.get('text') or '?') for a in actions[:20])
    more = f'\n… 共 {len(actions)} 条' if len(actions) > 20 else ''
    state_ = plan_data.get('state', 'pending')
    _send(token, chat_id,
          f'📋 <b>计划清单</b> ({len(actions)} 项 · {state_})\n\n{preview}{more}',
          ttl=LIST_MSG_TTL)


def _cb_clean_ask(token, chat_id, message_id, data):
    plan_id = data[10:]
    code, text = _plan_problem(plan_id)
    if code:
        _show_plan_problem(token, chat_id, message_id, code, text); return
    _edit(token, chat_id, message_id,
          f'⚠️ <b>确认执行清理？</b>\n\n计划 ID: <code>{th.h(plan_id)}</code>\n'
          f'将按清单删除本地 STRM（含 115 云端源）和分享 STRM。\n\n此操作不可撤销！',
          _clean_confirm_keyboard(plan_id), ttl=_plan_ttl_left(plan_id, BOT_MSG_TTL))


def _cb_clean_do(token, chat_id, message_id, data):
    plan_id = data[9:]
    # 执行前先核验：过期 / 已执行 / 规则变化 → 卡片就地提示并给「重新扫描」
    code, text = _plan_problem(plan_id)
    if code:
        _show_plan_problem(token, chat_id, message_id, code, text); return
    bm = _busy_msg()
    if bm:
        _edit(token, chat_id, message_id, bm, _close_keyboard(), ttl=20); return
    # 清理可能跑很久：进度卡片保留 30 分钟，别 60 秒就被删，否则结果没地方显示
    _edit(token, chat_id, message_id, '⏳ 正在执行清理...', {'inline_keyboard': []}, ttl=1800)
    _spawn_and_watch('clean', governance.action_inter_clean, chat_id,
                     message_id=message_id, plan=plan_id, dry_run=False,
                     notify=False)  # Bot 自己编辑确认消息，引擎不再另发一条


_CALLBACKS = [
    ('dismiss', _cb_dismiss),
    ('cmd:', _cb_cmd),
    ('plan:', _cb_plan),
    ('clean_ask:', _cb_clean_ask),
    ('clean_do:', _cb_clean_do),
]


# ═══════════════════ 长轮询 ═══════════════════
def _is_timeout(e):
    return isinstance(e, TimeoutError) or 'timed out' in str(e).lower()


def _note_poll_failure(e):
    _state['fail_count'] += 1
    _state['last_error'] = str(e) or type(e).__name__
    _state['last_error_ts'] = time.time()
    log.warning('getUpdates 失败(连续 %d 次): %s', _state['fail_count'], e)


def _handle_update(token, upd):
    try:
        if 'message' in upd: _handle_command(token, upd['message'])
        elif 'callback_query' in upd: _handle_callback(token, upd['callback_query'])
    except Exception as e:
        log.warning('处理 update 失败: %s', e)


def _submit_update(token, upd):
    """把 update 交给有界线程池；池满则记日志丢弃（不阻塞长轮询）。"""
    global _pending_updates
    with _POOL_LOCK:
        if _pending_updates >= MAX_PENDING_UPDATES:
            log.warning('更新线程池已满(%d)，丢弃 update %s',
                        MAX_PENDING_UPDATES, upd.get('update_id'))
            return False
        _pending_updates += 1

    def _run():
        global _pending_updates
        try:
            _handle_update(token, upd)
        finally:
            with _POOL_LOCK:
                _pending_updates -= 1

    try:
        _update_pool.submit(_run)
    except Exception:
        with _POOL_LOCK:
            _pending_updates -= 1
        raise
    return True


def _poll_loop(gen):
    log.info('Bot 长轮询线程启动')
    while _alive(gen):
        try:
            cfg = load_config()
            token = (cfg.get('telegram_bot_token') or '').strip()
            if not token:
                time.sleep(30); continue

            if not _state['initialized']:
                try:
                    me = tgmsg_transport.api(token, 'getMe', {}, timeout=10)
                    if me.get('ok'):
                        _state['bot_username'] = me['result'].get('username', '')
                        log.info('Bot 已连接: @%s', _state['bot_username'])
                        _init_bot(token)
                        _state['initialized'] = True
                except Exception as e:
                    log.warning('getMe 失败: %s', e)

            try:
                resp = tgmsg_transport.api(token, 'getUpdates', {
                    'offset': _state['offset'], 'timeout': 25,
                    'allowed_updates': json.dumps(['message', 'callback_query']),
                }, timeout=35)
            except Exception as e:
                _note_poll_failure(e)
                # 长轮询读超时在弱网 / 代理下很常见：立刻重连即可，别睡 5 秒（睡眠期间点的按钮全在排队）
                time.sleep(1 if _is_timeout(e) else min(3 * _state['fail_count'], 30))
                continue

            if not _alive(gen):
                break  # 长轮询期间被 restart() 换代：这批更新留给新线程处理，避免同一条命令执行两次
            if not resp.get('ok'):
                _note_poll_failure(RuntimeError(str(resp.get('description') or 'getUpdates 返回 not ok')))
                time.sleep(3); continue
            # 成功：清掉错误。以前 last_error 一旦出现就永远留着，Web 上一直红着「timed out」
            _state['last_poll'] = time.time()
            _state['fail_count'] = 0
            _state['last_error'] = ''

            for upd in resp.get('result', []):
                if not _alive(gen):
                    break  # 处理到一半被换代：剩下的留给新线程（offset 未推进），不重复执行
                _state['offset'] = upd['update_id'] + 1
                try:
                    storage.db_kv_set('tg_update_offset', str(_state['offset']))
                except Exception as e:
                    log.debug('保存 TG offset 失败: %s', e)
                # 去重闸：同一条 update 只派发一次（TG 在 offset 未确认时会重投）
                try:
                    uk = 'tg_upd_%s' % upd['update_id']
                    if storage.db_dedup_seen(uk):
                        continue
                    storage.db_dedup_add(uk)
                except Exception as e:
                    log.debug('TG update 去重失败: %s', e)
                # 有界线程池：慢操作（网络卡住的 edit / delete）不再堵住后面所有按钮点击，
                # 同时不会为每条消息起一个线程。
                _submit_update(token, upd)

        except Exception as e:
            _note_poll_failure(e)
            time.sleep(5)

    log.info('Bot 长轮询线程退出')


def start():
    with _LOCK:  # 两次配置保存同时触发 restart 时，不能读到同一个 gen 起出两个轮询线程
        _start_locked()


def _start_locked():
    if _state['running']: return
    token = (load_config().get('telegram_bot_token') or '').strip()
    _state['token'] = token  # 清理线程用缓存的 token：清空配置时也要跟着清掉
    if not token:
        log.info('Telegram Bot Token 未配置，跳过启动'); return
    if not _state['queue_loaded']:
        _load_queue()
        _state['queue_loaded'] = True
    # 进程重启后从 SQLite 恢复 update offset：不再重复处理重启前的旧消息
    # （此前 offset 只在内存里，重启即归零，TG 会把未确认的旧 update 再推一遍）
    try:
        persisted = int(storage.db_kv_get('tg_update_offset', '0') or 0)
        if persisted > (_state.get('offset') or 0):
            _state['offset'] = persisted
    except Exception as e:
        log.debug('恢复 TG offset 失败: %s', e)
    _state['running'] = True
    _state['gen'] += 1
    gen = _state['gen']
    t = threading.Thread(target=_poll_loop, args=(gen,), daemon=True, name='tg-bot')
    t.start()
    _state['thread'] = t
    c = threading.Thread(target=_cleanup_loop, args=(gen,), daemon=True, name='tg-cleanup')
    c.start()
    log.info('Telegram Bot 已启动')


def stop():
    _state['running'] = False


def restart():
    with _LOCK:
        _restart_locked()


def _restart_locked():
    stop()
    # 重置连接状态，让 _poll_loop 重新 getMe + setMyCommands，
    # 否则改了 Bot Token 后用户名/命令列表会停留在旧值。
    _state['initialized'] = False
    _state['bot_username'] = ''
    _state['last_error'] = ''
    _state['fail_count'] = 0
    # offset 保留：避免重启后重复处理旧消息。
    # 以前靠 sleep(0.5) 等旧线程退出，但旧线程正卡在 25 秒的长轮询里，根本来不及看到 running=False，
    # 每保存一次配置就多一个轮询线程；现在靠代数 gen 让旧线程自行退出。
    _start_locked()


def status():
    with _DELETE_LOCK:
        pending = len(_delete_queue)
    now = time.time()
    ago = int(now - _state['last_poll']) if _state['last_poll'] else None
    fails = _state['fail_count']
    t = _state['thread']
    alive = bool(t and t.is_alive())
    # state: stopped 未启动 / starting 刚启动 / ok 正常 / degraded 网络不稳（在重试）/ down 无响应
    if not _state['running']:
        st = 'stopped'
    elif not alive or (ago is not None and ago > 180) or (ago is None and fails >= 3):
        st = 'down'
    elif fails >= 1 or (ago is not None and ago > 60):
        st = 'degraded'
    elif ago is None:
        st = 'starting'
    else:
        st = 'ok'
    return {
        'running': _state['running'],
        'state': st,
        'thread_alive': alive,
        'bot_username': _state['bot_username'],
        'last_poll_ago': ago,
        'fail_count': fails,
        # 只在「当前仍在失败」时给出错误；已恢复就是空，Web 不再显示过期的报错
        'last_error': _state['last_error'] if fails else '',
        'last_error_ago': int(now - _state['last_error_ts']) if (fails and _state['last_error_ts']) else None,
        'pending_deletes': pending,
    }
