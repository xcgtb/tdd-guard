# -*- coding: utf-8 -*-
"""唯一直接对 Telegram Bot API 发起 HTTP 请求的模块。

``parse_mode='HTML'`` 只允许出现在这里（验收会 grep）；Bot 轮询、阅后即焚、
网页「测试 Telegram」、订阅推送全部走本模块。``notify()`` 读取运行时配置并自动切段。
"""
import json
import logging
import urllib.parse
import urllib.request
import urllib.error

from . import html as th
from .. import state

log = logging.getLogger('media_agent')
_API = 'https://api.telegram.org/bot{token}/{method}'


def api(token, method, params=None, timeout=35):
    url = _API.format(token=token, method=method)
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(url, data=data, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def err_desc(e):
    """Telegram 的 4xx 会抛 HTTPError，真正的原因在响应体 description 里。"""
    if isinstance(e, urllib.error.HTTPError):
        try:
            return json.loads(e.read().decode('utf-8')).get('description', '') or str(e)
        except Exception:
            return str(e)
    return str(e)


def send(token, chat_id, text, keyboard=None, reply_to=None, timeout=15):
    """发新消息，返回 Telegram 原始响应（失败返回 None）。超长自动安全截断。"""
    params = {
        'chat_id': str(chat_id), 'text': th.safe_truncate(text, 4096),
        'parse_mode': 'HTML', 'disable_web_page_preview': 'true',
    }
    if reply_to:
        params['reply_to_message_id'] = reply_to
    if keyboard is not None:
        params['reply_markup'] = json.dumps(keyboard)
    try:
        return api(token, 'sendMessage', params, timeout=timeout)
    except Exception as e:
        log.warning('Telegram 发送失败: %s', e)
        return None


def edit(token, chat_id, message_id, text, keyboard=None, timeout=15):
    """就地编辑消息；内容未变化（'message is not modified'）视为成功。"""
    params = {
        'chat_id': str(chat_id), 'message_id': message_id,
        'text': th.safe_truncate(text, 4096),
        'parse_mode': 'HTML', 'disable_web_page_preview': 'true',
    }
    if keyboard is not None:
        params['reply_markup'] = json.dumps(keyboard)
    try:
        return api(token, 'editMessageText', params, timeout=timeout)
    except Exception as e:
        desc = err_desc(e)
        if 'not modified' in desc.lower():
            return {'ok': True}
        log.warning('编辑消息失败: %s', desc)
        return None


def delete(token, chat_id, message_id, timeout=10):
    """删除消息；失败向上抛，由调用方（阅后即焚队列）决定是否重试。"""
    return api(token, 'deleteMessage',
               {'chat_id': str(chat_id), 'message_id': message_id}, timeout=timeout)


def answer_cb(token, cb_id, text='', alert=False, timeout=10):
    """应答回调查询（按钮去转圈 / 弹提示）。"""
    try:
        return api(token, 'answerCallbackQuery', {
            'callback_query_id': cb_id, 'text': th.safe_truncate(text, 200),
            'show_alert': 'true' if alert else 'false',
        }, timeout=timeout)
    except Exception as e:
        log.warning('应答按钮失败: %s', e)
        return None


def notify(text, chat_id=None):
    """按运行时配置推送一段（可能超长的）HTML，自动切段；返回是否全部成功。"""
    cfg = state.RUNTIME_CFG
    token = (cfg.get('telegram_bot_token') or '').strip()
    cid = chat_id or (cfg.get('telegram_chat_id') or '').strip()
    if not token or not cid:
        return False
    ok_all = True
    for part in th.split_telegram_html(text):
        r = send(token, cid, part, timeout=10)
        ok_all = ok_all and bool(r and r.get('ok'))
    return ok_all


def send_test(token, chat_id):
    """网页「测试 Telegram」用：发一条测试消息，返回 (ok, description)。"""
    text = '✅ <b>TTD Guard</b> 测试消息\n如果你看到这条消息，说明 Telegram 通知已配置成功。'
    try:
        resp = api(token, 'sendMessage',
                   {'chat_id': str(chat_id), 'text': text, 'parse_mode': 'HTML'}, timeout=10)
    except Exception as e:
        return False, str(e)
    if resp.get('ok'):
        return True, '已发送测试消息'
    return False, (resp.get('description') or '未知错误')
