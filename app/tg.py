# -*- coding: utf-8 -*-
"""兼容壳：Telegram 消息层的实现已全部搬到 app/tgmsg/。

只把旧名字再导出一次，供尚未迁移的历史代码 / 测试使用：
  tg_title / tg_row / tg_stamp / fmt_scan_text / split_telegram_html / notify_telegram
新代码请直接用 ``from .tgmsg import blocks / html / reports / transport``。
"""
from .tgmsg import blocks as _blocks
from .tgmsg import html as _html
from .tgmsg import transport as _transport

tg_title = _blocks.tg_title
tg_row = _blocks.tg_row
tg_stamp = _blocks.tg_stamp
fmt_scan_text = _blocks.fmt_scan_text
split_telegram_html = _html.split_telegram_html


def notify_telegram(text, chat_id=None):
    return _transport.notify(text, chat_id=chat_id)
