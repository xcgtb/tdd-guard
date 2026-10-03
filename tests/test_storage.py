# -*- coding: utf-8 -*-
"""SQLite 存储层（app/storage.py）测试：计划 / 审计 / 订阅状态 / 文档 / TMDB 缓存 / 迁移 / 配置写锁。

每个测试经 isolated_state 夹具拿到独立的 STATE_DIR（= 独立的库）。
"""
import json
import logging
import sqlite3
import threading
import time

import pytest

from app import config, governance, logger, storage, subscribe, tmdb


def _plan(pid='abcd1234', state='pending', ts=None):
    return {'schema_version': 2, 'id': pid, 'ts': ts or time.time(), 'rule_sig': 'sig',
            'state': state, 'stats': {'loc': 1}, 'actions': [], 'executed_at': None,
            'executed_result': None}


@pytest.fixture(autouse=True)
def _iso(isolated_state):
    yield isolated_state


class TestPlanStorage:
    def test_plan_roundtrip_and_state_update(self):
        assert storage.db_save_plan(_plan('aaaa1111'))
        assert governance.load_plan('aaaa1111')['state'] == 'pending'
        assert governance.save_plan_state('aaaa1111', 'done', {'executed_at': 5})
        p = governance.load_plan('aaaa1111')
        assert p['state'] == 'done' and p['executed_at'] == 5
        assert storage.db_list_plans(5)[0]['state'] == 'done'
        assert governance.save_plan_state('ffff0000', 'done') is False

    def test_purge_old_plans(self):
        storage.db_save_plan(_plan('cccc3333', ts=time.time() - 30 * 86400))
        storage.db_purge_plans(time.time() - 7 * 86400)
        assert storage.db_load_plan('cccc3333') is None

    def test_latest_pending_plan(self):
        now = time.time()
        storage.db_save_plan(_plan('aaaa0001', ts=now - 100))
        storage.db_save_plan(_plan('aaaa0002', ts=now - 50, state='done'))
        storage.db_save_plan(_plan('aaaa0003', ts=now - 99999))
        assert storage.db_latest_pending_plan(3600) == 'aaaa0001'
        assert storage.db_latest_pending_plan(10) is None


class TestAuditStorage:
    def test_audit_roundtrip_newest_first(self):
        for i in range(3):
            storage.db_add_audit({'ts': '2026-01-01 00:00:0%d' % i,
                                  'category': 'c', 'title': 't%d' % i, 'details': []})
        rows = storage.db_recent_audit(2)
        assert [r['title'] for r in rows] == ['t2', 't1']

    def test_logger_writes_db_only(self, isolated_state):
        logger.write('cat', 'title', ['d1'])
        assert logger.read_recent(1)[0]['details'] == ['d1']
        assert not list(isolated_state.parent.rglob('*.jsonl'))


class TestSubStateStorage:
    def test_roundtrip_and_reset(self):
        subscribe._save_sub_state({'s1': {'tmdb_total': 10}})
        assert subscribe._load_sub_state()['s1']['tmdb_total'] == 10
        subscribe.reset_sub_state()
        assert subscribe._load_sub_state() == {}

    def test_subscriptions_table_and_config_compat(self):
        subs = [{'id': 'a', 'name': 'A'}, {'id': 'b', 'name': 'B'}, {'id': 'a', 'name': 'A2'}]
        assert subscribe.set_subscriptions(subs) == subs
        assert [s['name'] for s in subscribe.get_subscriptions()] == ['A', 'B', 'A2']
        assert config.get_subscriptions() == subscribe.get_subscriptions()
        config.set_subscriptions([])
        assert subscribe.get_subscriptions() == []
        assert 'subscriptions' not in config.load_config()


class TestDedupAndKv:
    def test_dedup_and_kv(self):
        assert not storage.db_dedup_seen('tg_upd_1')
        storage.db_dedup_add('tg_upd_1')
        assert storage.db_dedup_seen('tg_upd_1')
        storage.db_kv_set('tg_update_offset', '42')
        assert storage.db_kv_get('tg_update_offset', '0') == '42'

    def test_kv_incr_atomic(self):
        ts = [threading.Thread(target=lambda: [storage.db_kv_incr('bus') for _ in range(10)]) for _ in range(10)]
        for t in ts: t.start()
        for t in ts: t.join()
        assert storage.db_kv_get('bus') == '100'


class TestDocs:
    def test_version_increments(self):
        assert storage.db_doc_meta('x') is None
        assert storage.db_doc_get('x', 'dflt') == 'dflt'
        assert storage.db_doc_put('x', {'a': 1}) == 1
        assert storage.db_doc_put('x', {'a': 2}) == 2
        meta = storage.db_doc_meta('x')
        assert meta['version'] == 2 and meta['updated_at'] > 0
        assert storage.db_doc_get('x') == {'a': 2}
        assert storage.db_doc_update('x', lambda d: d.update(b=3)) == {'a': 2, 'b': 3}
        assert storage.db_doc_meta('x')['version'] == 3
        storage.db_doc_delete('x')
        assert storage.db_doc_get('x') is None

    def test_update_atomic_under_threads(self):
        def bump():
            for _ in range(10):
                storage.db_doc_update('counter', lambda d: {'n': d['n'] + 1}, {'n': 0})
        ts = [threading.Thread(target=bump) for _ in range(20)]
        for t in ts: t.start()
        for t in ts: t.join()
        assert storage.db_doc_get('counter') == {'n': 200}
        assert storage.db_doc_meta('counter')['version'] == 200


class TestTmdbCache:
    def test_two_instances_both_persist(self, monkeypatch):
        calls = []

        class _Resp:
            def __init__(self, body): self.body = body
            def read(self): return self.body
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=15):
            calls.append(req.full_url)
            return _Resp(json.dumps({'url': req.full_url}).encode())
        monkeypatch.setattr(tmdb.urllib.request, 'urlopen', fake_urlopen)
        monkeypatch.setattr(tmdb.time, 'sleep', lambda s: None)
        t1, t2 = tmdb.Tmdb(), tmdb.Tmdb()
        t1.get('/tv/1')
        t2.get('/tv/2')
        t1.save(); t2.save()
        t3 = tmdb.Tmdb()
        assert t3.get('/tv/1')['url'].startswith(tmdb.TMDB_BASE + '/tv/1')
        assert t3.get('/tv/2')['url'].startswith(tmdb.TMDB_BASE + '/tv/2')
        assert len(calls) == 2 and t3.hits == 2
        # ttl 过期 → 不命中
        assert storage.db_tmdb_get('/tv/1?language=' + tmdb.TMDB_LANG, 0) is None

    def test_prune(self):
        storage.db_tmdb_put('old', {'a': 1}, ts=time.time() - 2 * 86400)
        storage.db_tmdb_put('new', {'a': 2})
        assert storage.db_tmdb_prune() == 1
        assert storage.db_tmdb_get('new', 60) == {'a': 2}


class TestErrorPath:
    def test_closed_connection_logs_warning(self, caplog):
        storage.db_kv_set('k', 'v')
        for con in storage._DBS.values():
            con.close()
        with caplog.at_level(logging.WARNING, logger='media_agent'):
            assert storage.db_kv_get('k', 'dflt') == 'dflt'
            assert storage.db_doc_put('d', {}) == 0
            assert storage.db_doc_get('d', 7) == 7
            assert storage.db_kv_get('k', 'dflt') == 'dflt'   # 同一 op 60 秒内只告警一次
        msgs = [r.getMessage() for r in caplog.records if '存储层' in r.getMessage()]
        assert len([m for m in msgs if 'kv_get' in m]) == 1
        assert any('doc_put' in m for m in msgs)
        storage.close_all()
        assert storage.db_kv_get('k') == 'v'          # 关掉后重新打开，数据还在

    def test_corrupt_payload_returns_default(self, caplog):
        storage._execute('INSERT INTO docs (name, payload, version, updated_at) VALUES (?,?,?,?)',
                         ('bad', '{broken', 1, time.time()))
        with caplog.at_level(logging.WARNING, logger='media_agent'):
            assert storage.db_doc_get('bad', 'dflt') == 'dflt'
            # 损坏的旧值按 default 重建，不能永久卡死更新
            assert storage.db_doc_update('bad', lambda d: d + [1], []) == [1]
        assert any('doc_get:bad' in r.getMessage() for r in caplog.records)
        assert storage.db_doc_get('bad') == [1]


class TestMigration:
    def _legacy(self, sd):
        data = sd.parent
        (data / 'records').mkdir(parents=True, exist_ok=True)
        (sd / 'plan_aaaa1111.json').write_text(json.dumps(_plan('aaaa1111')), encoding='utf-8')
        (sd / 'plan_bbbb2222.json').write_text('{broken', encoding='utf-8')
        (sd / 'subscriptions_state.json').write_text(json.dumps({'s1': {'tmdb_total': 3}}), encoding='utf-8')
        (data / 'records' / 'audit.20260101-000000.jsonl').write_text(
            json.dumps({'ts': '2026-01-01 00:00:00', 'category': 'c', 'title': 'old'}) + '\n', encoding='utf-8')
        (data / 'records' / 'audit.jsonl').write_text(
            json.dumps({'ts': '2026-02-01 00:00:00', 'category': 'c', 'title': 'new'}) + '\nbad line\n',
            encoding='utf-8')
        (sd / 'tmdb_cache.json').write_text(json.dumps({
            'fresh': {'ts': time.time(), 'data': {'x': 1}},
            'stale': {'ts': time.time() - 3 * 86400, 'data': {'x': 2}}}), encoding='utf-8')
        docs = {}
        for name, (base, fname) in storage.LEGACY_DOC_FILES.items():
            p = (sd if base == 'state' else data) / fname
            docs[name] = {'doc': name, 'ts': 1.0}
            p.write_text(json.dumps(docs[name]), encoding='utf-8')
        return docs

    def test_migrates_everything_once(self, isolated_state, monkeypatch, tmp_path):
        sd = isolated_state
        cfg_file = tmp_path / 'config.json'
        cfg_file.write_text(json.dumps({'emby_host': 'http://h', 'subscriptions': json.dumps(
            [{'id': 'a', 'name': 'A'}, {'id': 'b', 'name': 'B'}])}), encoding='utf-8')
        monkeypatch.setattr(config, 'CONFIG_FILE', cfg_file)
        monkeypatch.setattr(config, '_CFG_CACHE', {'sig': None, 'data': None})
        docs = self._legacy(sd)

        s = storage.db_migrate()
        assert s['plans'] == 1 and s['sub_state'] == 1 and s['audit'] == 2
        assert s['tmdb'] == 2 and s['docs'] == len(storage.LEGACY_DOC_FILES) and s['subscriptions'] == 2
        assert storage.db_load_plan('aaaa1111')['id'] == 'aaaa1111'
        assert subscribe._load_sub_state() == {'s1': {'tmdb_total': 3}}
        assert [r['title'] for r in storage.db_recent_audit(5)] == ['new', 'old']
        assert storage.db_tmdb_get('fresh', 3600) == {'x': 1}
        assert storage.db_tmdb_get('stale', 10 ** 9) is None          # 迁移后按 24h 清理
        for name, val in docs.items():
            assert storage.db_doc_get(name) == val
        assert [x['id'] for x in subscribe.get_subscriptions()] == ['a', 'b']
        on_disk = json.loads(cfg_file.read_text(encoding='utf-8'))
        assert 'subscriptions' not in on_disk and on_disk['emby_host'] == 'http://h'
        # 旧文件全部改名 *.migrated，state/ 下不再有 *.json
        assert not list(sd.glob('*.json'))
        assert not list((sd.parent / 'records').glob('*.jsonl'))
        assert (sd / 'plan_aaaa1111.json.migrated').exists()
        assert (sd.parent / 'gov_auto.json.migrated').exists()

        # 第二次：无事可做；即便旧文件又出现（例如改名失败的情形）也不会重复导入
        (sd / 'subscriptions_state.json').write_text(json.dumps({'zz': {}}), encoding='utf-8')
        (sd.parent / 'records' / 'audit.jsonl').write_text(
            json.dumps({'ts': '2026-03-01 00:00:00', 'title': 'again'}) + '\n', encoding='utf-8')
        s2 = storage.db_migrate()
        assert not any(v for k, v in s2.items() if k != 'errors') and not s2['errors']
        assert subscribe._load_sub_state() == {'s1': {'tmdb_total': 3}}
        assert storage.db_audit_count() == 2

    def test_old_audit_flag_skips_already_imported(self, isolated_state):
        rec = isolated_state.parent / 'records'
        rec.mkdir(parents=True, exist_ok=True)
        (rec / 'audit.jsonl').write_text(json.dumps({'ts': '2026-02-01 00:00:00', 'title': 'dup'}) + '\n',
                                         encoding='utf-8')
        storage.db_add_audit({'ts': '2026-02-01 00:00:00', 'title': 'dup'})
        storage.db_kv_set('audit_jsonl_imported', str(int(time.time()) + 10))
        storage.db_migrate()
        assert storage.db_audit_count() == 1
        assert (rec / 'audit.jsonl.migrated').exists()

    def test_missing_files_just_set_flags(self):
        s = storage.db_migrate()
        assert s['errors'] == [] and storage.db_kv_get('migrated:sub_state')


class TestConfigLock:
    def test_concurrent_update_config_loses_nothing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, 'CONFIG_FILE', tmp_path / 'config.json')
        monkeypatch.setattr(config, 'DATA_DIR', tmp_path)
        monkeypatch.setattr(config, '_CFG_CACHE', {'sig': None, 'data': None})
        keys = ['morning_report_hour', 'morning_report_minute', 'ingest_interval_min', 'subscribe_interval_min',
                'tmdb_scan_last_ts', 'morning_prescan_min', 'strategy_season_ratio', 'ingest_quiet_minutes',
                'tmdb_scan_interval_hours', 'morning_report_items', 'strategy_exempt_keywords',
                'morning_report_last_date', 'morning_prescan_last_date', 'strategy_decision',
                'match_strategy', 'strategy_special_action', 'strategy_cover', 'subscribe_enabled',
                'ingest_enabled', 'morning_report_enabled']
        assert len(keys) == 20
        barrier = threading.Barrier(20)

        def worker(k):
            barrier.wait()
            config.update_config(lambda c: c.__setitem__(k, 'v-' + k))
        ts = [threading.Thread(target=worker, args=(k,)) for k in keys]
        for t in ts: t.start()
        for t in ts: t.join()
        saved = json.loads((tmp_path / 'config.json').read_text(encoding='utf-8'))
        assert all(saved[k] == 'v-' + k for k in keys)
