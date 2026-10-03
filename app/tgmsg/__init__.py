# -*- coding: utf-8 -*-
"""Telegram 消息层：HTML 转义/校验（html）、排版积木（blocks）、卡片渲染（reports）、发送（transport）。

业务模块统一用 ``from .tgmsg import xxx`` / ``from .tgmsg import html as th`` 访问；
``app/tg.py`` 只是把旧名字再导出一次的兼容壳。
"""
from . import html, blocks, reports, transport  # noqa: F401
