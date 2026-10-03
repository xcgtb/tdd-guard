# -*- coding: utf-8 -*-
"""SQLite 存储层（app/storage.py）测试：计划兜底 / 审计主读 / 订阅状态自愈 / 去重与 offset。

运行方式同其它测试：`python3 tests/run_engine_tests.py` 或 `pytest tests/ -v`。
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix='ttdguard_storage_test_'))
os.environ['L_ROOT'] = str(_TMP / 'local')
os.environ['S_ROOT'] = str(_TMP / 'share')
os.environ['CLOUD_L_ROOT'] = str(_TMP / 'cloud')
os.environ['AGENT_DATA'] = str(_TMP / 'data')
os.environ['TMDB_KEY'] = ''
os.environ['TG_BOT_TOKEN'] = ''

sys.path.insert(0, str(Path(__file__).parent.parent / 'app'))
import engine  # noqa: E402
import governance  # noqa: E402
import subscribe  # noqa: E402


def _plan(pid='abcd1234', state='pending', ts=None):
    return {'schema_version': 2, 'id': pid, 'ts': ts or time.time(), 'rule_sig': 'sig',
            'state': state, 'stats': {'loc': 1}, 'actions': [], 'executed_at': None,
            'executed_result': None}


class TestPlanStorage:
    def setup_method(self):
        engine.STATE_DIR.mkdir(parents=True, exist_ok=True)

    def test_plan_file_removed_falls_back_to_db(self):
        p = _plan('aaaa1111')
        engine.db_save_plan(p)
        assert engine.db_load_plan('aaaa1111')['id'] == 'aaaa1111'
        # 文件不存在 → governance.load_plan 走库兜底
        assert governance.load_plan('aaaa1111')['state'] == 'pending'

    def test_file_wins_over_db(self):
        engine.db_save_plan(_plan('bbbb2222', state='pending'))
        f = engine.STATE_DIR / 'plan_bbbb2222.json'
        f.write_text(json.dumps(_plan('bbbb2222', state='done')), encoding='utf-8')
        assert governance.load_plan('bbbb2222')['state'] == 'done'

    def test_purge_old_plans(self):
        engine.db_save_plan(_plan('cccc3333', ts=time.time() - 30 * 86400))
        engine.db_purge_plans(time.time() - 7 * 86400)
        assert engine.db_load_plan('cccc3333') is None


class TestAuditStorage:
    def test_audit_roundtrip_newest_first(self):
        for i in range(3):
            engine.db_add_audit({'ts': '2026-01-01 00:00:0%d' % i, 'ts_epoch': 1000 + i,
                                 'category': 'c', 'title': 't%d' % i, 'details': []})
        rows = engine.db_recent_audit(2)
        assert [r['title'] for r in rows] == ['t2', 't1']


class TestSubStateStorage:
    def test_corrupt_file_heals_from_db(self):
        p = engine.SUB_STATE_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        subscribe._save_sub_state({'s1': {'tmdb_total': 10}})
        p.write_text('{broken', encoding='utf-8')
        st = subscribe._load_sub_state()
        assert st['s1']['tmdb_total'] == 10
        assert json.loads(p.read_text(encoding='utf-8'))['s1']['tmdb_total'] == 10  # 回写

    def test_missing_file_is_reset_not_resurrected(self):
        subscribe._save_sub_state({'s2': {'tmdb_total': 5}})
        engine.SUB_STATE_FILE.unlink()
        assert subscribe._load_sub_state() == {}
        assert subscribe._load_sub_state() == {}


class TestDedupAndKv:
    def test_dedup_and_kv(self):
        assert not engine.db_dedup_seen('tg_upd_1')
        engine.db_dedup_add('tg_upd_1')
        assert engine.db_dedup_seen('tg_upd_1')
        engine.db_kv_set('tg_update_offset', '42')
        assert engine.db_kv_get('tg_update_offset', '0') == '42'
