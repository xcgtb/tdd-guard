# -*- coding: utf-8 -*-
"""
bot.py 测试：Telegram 白名单鉴权（_is_allowed）。

这是 Telegram 输入直接进来的那一层，必须保证：
  - 没配置白名单时默认拒绝所有人（v4.4 修的那个 bug，防止回归）
  - 配置了白名单后，user_id 或 chat_id 命中其一即可
  - 没命中的 id 要被拒绝
"""
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix='ttdguard_test_bot_'))
os.environ['L_ROOT'] = str(_TMP / 'local')
os.environ['S_ROOT'] = str(_TMP / 'share')
os.environ['CLOUD_L_ROOT'] = str(_TMP / 'cloud')
os.environ['AGENT_DATA'] = str(_TMP / 'data')
os.environ['TMDB_KEY'] = ''
os.environ['TG_BOT_TOKEN'] = ''
os.environ['TG_ALLOWED_USERS'] = ''  # 白名单从 config.json 读，不从这个环境变量的默认值读

sys.path.insert(0, str(Path(__file__).parent.parent / 'app'))
import bot  # noqa: E402
import config  # noqa: E402


class TestIsAllowed:
    def test_default_deny_when_no_allowlist_configured(self):
        config.save_config({'telegram_allowed_users': ''})
        assert bot._is_allowed('111', '222') is False
        assert bot._is_allowed(None, None) is False

    def test_allows_matching_user_id(self):
        config.save_config({'telegram_allowed_users': '111,222'})
        assert bot._is_allowed('111', '999') is True

    def test_allows_matching_chat_id(self):
        config.save_config({'telegram_allowed_users': '111,222'})
        assert bot._is_allowed('999', '222') is True

    def test_rejects_id_not_in_allowlist(self):
        config.save_config({'telegram_allowed_users': '111,222'})
        assert bot._is_allowed('333', '444') is False

    def test_ignores_blank_entries_in_allowlist(self):
        config.save_config({'telegram_allowed_users': '111,, ,222,'})
        assert bot._is_allowed('111', None) is True
        assert bot._is_allowed('222', None) is True
        assert bot._is_allowed('', None) is False
