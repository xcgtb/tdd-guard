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


# ═══════════════════ 线程生命周期 / 结果渲染 / 计划选择 ═══════════════════
import json as _json  # noqa: E402
import threading as _threading  # noqa: E402
import time as _time  # noqa: E402


def _alive(name):
    return sum(1 for t in _threading.enumerate() if t.name == name and t.is_alive())


class TestRestartDoesNotLeakPollers:
    def test_restart_keeps_exactly_one_poll_loop(self):
        """以前 restart() 只 sleep(0.5) 就重新 start()，旧线程还卡在长轮询里，
        每在网页保存一次配置就多出一个 getUpdates 循环，同一条命令可能被处理两次"""
        calls = []

        def fake_api(token, method, params=None, timeout=35):
            calls.append(method)
            if method == 'getUpdates':
                _time.sleep(0.3)  # 模拟长轮询
                return {'ok': True, 'result': []}
            if method == 'getMe':
                return {'ok': True, 'result': {'username': 'test_bot'}}
            return {'ok': True, 'result': {}}

        orig_api, orig_cfg = bot._api, bot.load_config
        bot._api = fake_api
        bot.load_config = lambda: {'telegram_bot_token': 'x', 'telegram_allowed_users': ''}
        try:
            bot.start()
            for _ in range(3):
                bot.restart()
            _time.sleep(2.5)
            assert _alive('tg-bot') == 1
            assert _alive('tg-cleanup') == 1
            assert 'getUpdates' in calls
        finally:
            bot.stop()
            _time.sleep(2.5)
            bot._api, bot.load_config = orig_api, orig_cfg
        assert _alive('tg-bot') == 0


class TestCleanResultRendering:
    def test_skipped_count_is_int(self):
        """引擎返回的 skipped 是跳过项数（int），以前按列表 len() 会在清理完成后抛错"""
        sent = []
        orig = bot._edit, bot._send
        bot._edit = lambda token, chat_id, mid, text, kb=None: sent.append(text)
        bot._send = lambda token, chat_id, text, *a, **k: sent.append(text)
        try:
            bot._notify_result('tok', 1, 'clean',
                               {'status': 'success', 'loc_cnt': 2, 'sh_cnt': 1,
                                'refreshed': True, 'skipped': 3}, message_id=9)
        finally:
            bot._edit, bot._send = orig
        assert len(sent) == 1
        assert '跳过' in sent[0] and '3' in sent[0]


class TestLatestPendingPlan:
    def test_picks_newest_pending_not_newest_file(self):
        """计划被标记 done/expired 时会重写文件、mtime 变新；/clean 不能因此选中已执行过的计划"""
        engine = bot.engine
        engine.STATE_DIR.mkdir(parents=True, exist_ok=True)
        for f in engine.STATE_DIR.glob('plan_*.json'):
            f.unlink()
        now = _time.time()

        def write(pid, state, ts):
            (engine.STATE_DIR / f'plan_{pid}.json').write_text(_json.dumps({
                'schema_version': 2, 'id': pid, 'ts': ts, 'state': state,
                'stats': {}, 'actions': []}), encoding='utf-8')
        write('aaaa1111', 'pending', now - 100)
        write('cccc3333', 'pending', now - engine.PLAN_TTL - 60)  # 已过期
        write('bbbb2222', 'done', now - 50)                        # 最后写入、mtime 最新
        assert bot._latest_pending_plan_id() == 'aaaa1111'
        for f in engine.STATE_DIR.glob('plan_*.json'):
            f.unlink()
        assert bot._latest_pending_plan_id() is None
