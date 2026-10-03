# -*- coding: utf-8 -*-
"""缓存失效总线 / 多端同步 测试。

覆盖：
  - bump 版本号自增 + IMPLIES 展开
  - 单个失效回调抛异常不影响其它回调
  - check_external 发现「其它进程」写的版本号并补跑回调
  - stats.invalidate_media_caches bump library、清空内存缓存并给磁盘缓存打 stale 标记
  - GET /api/sync/versions 鉴权（401 / 200）
  - POST /api/manual_done bump library
  - 真实 action_inter_clean 路径 bump library + plans
  - 前端同步 JS 块存在且检查 document.hidden / visibilityState
"""
import os
import shutil
from pathlib import Path

# 与 tests/test_main.py 一致：app.main / deps 在导入时读 WEB_PASSWORD
os.environ.setdefault('WEB_USER', 'admin')
os.environ.setdefault('WEB_PASSWORD', 'test-pass-123')

import pytest

from app import state, storage, sync, stats, lib, governance

_AUTH = ('admin', 'test-pass-123')
_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_sync(monkeypatch):
    """每个用例一份干净的「已见版本」表和 stale 标记；回调表按需单独 patch。"""
    monkeypatch.setattr(sync, '_SEEN', {})
    monkeypatch.setattr(state, '_stale', set())


def _seed_caches():
    state._ep_cache.update({'ts': 1, 'data': {'x': 1}})
    state._emby_index_cache.update({'ts': 1, 'data': {'x': 1}})
    state._emby_lib_cache.update({'ts': 1, 'data': {'x': 1}})
    state._live_eps_cache['k'] = (1, {})
    state._strm_count_cache.update({'ts': 1, 'local': 5, 'share': 6})
    state._lib_stats_cache.update({'ts': 1, 'data': {'rows': []}})


class TestBump:
    def test_increment_and_implies(self, isolated_state):
        assert sync.versions()['library'] == 0
        v = sync.bump('library')
        assert v['library'] == 1
        assert v['explore'] == 1                 # IMPLIES: library -> explore
        assert sync.versions()['explore'] == 1
        v2 = sync.bump('library')
        assert v2['library'] == 2 and v2['explore'] == 2
        assert sync.versions()['library'] == 2

    def test_plans_implies_governance(self, isolated_state):
        v = sync.bump('plans')
        assert v['plans'] == 1 and v['governance'] == 1

    def test_callback_error_does_not_stop_others(self, isolated_state, monkeypatch):
        ran = []

        def boom():
            raise RuntimeError('回调坏了')

        def ok():
            ran.append(1)

        monkeypatch.setattr(sync, '_CALLBACKS', {'ingest': [boom, ok]})
        sync.bump('ingest')
        assert ran == [1]


class TestCheckExternal:
    def test_runs_callbacks_when_db_bumped_by_other_process(self, isolated_state, monkeypatch):
        ran = []
        monkeypatch.setattr(sync, '_CALLBACKS', {'records': [lambda: ran.append(1)]})
        sync.check_external()                     # 首次调用：只认领版本号，不补跑回调
        assert ran == []
        storage.db_kv_incr('sync:records')        # 模拟另一个进程（如 CLI）写库
        sync.check_external()
        assert ran == [1]
        sync.check_external()                     # 已 seen，不重复触发
        assert ran == [1]


class TestInvalidateMedia:
    def test_invalidate_media_caches_bumps_and_clears(self, isolated_state):
        _seed_caches()
        stats.invalidate_media_caches()
        assert sync.versions()['library'] >= 1
        assert state._ep_cache['data'] is None
        assert state._emby_index_cache['data'] is None
        assert state._emby_lib_cache['data'] is None
        assert state._live_eps_cache == {}
        assert state._strm_count_cache['ts'] == 0
        assert state._lib_stats_cache['data'] is None
        assert 'emby_index' in state._stale and 'emby_overview' in state._stale

    def test_keep_emby_lib_preserves_overview(self, isolated_state):
        _seed_caches()
        saved = dict(state._emby_lib_cache['data'])
        stats.invalidate_media_caches(keep_emby_lib=True)
        assert state._emby_lib_cache['data'] == saved   # 就地修补流程依赖它不被清掉
        assert 'emby_overview' not in state._stale
        assert state._ep_cache['data'] is None
        assert sync.versions()['library'] >= 1

    def test_invalidate_stats_cache_bumps(self, isolated_state):
        _seed_caches()
        stats.invalidate_stats_cache()
        assert state._strm_count_cache['ts'] == 0
        assert state._lib_stats_cache['data'] is None
        assert sync.versions()['library'] >= 1


class TestSyncRoute:
    def test_versions_requires_auth(self, isolated_state):
        from fastapi.testclient import TestClient
        from app.main import app as main_app
        c = TestClient(main_app)
        assert c.get('/api/sync/versions').status_code == 401

    def test_versions_ok_with_auth(self, isolated_state):
        from fastapi.testclient import TestClient
        from app.main import app as main_app
        c = TestClient(main_app)
        r = c.get('/api/sync/versions', auth=_AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body['status'] == 'success'
        assert set(sync.DOMAINS) <= set(body['v'])
        assert 'ts' in body

    def test_manual_done_bumps_library(self, isolated_state):
        from fastapi.testclient import TestClient
        from app.main import app as main_app
        c = TestClient(main_app)
        before = sync.versions()['library']
        r = c.post('/api/manual_done', json={'id': 'tv:1', 'name': '某剧', 'done': True}, auth=_AUTH)
        assert r.status_code == 200 and r.json()['status'] == 'success'
        assert sync.versions()['library'] > before


class _Args:
    def __init__(self, plan='', dry_run=False):
        self.plan = plan; self.dry_run = dry_run; self.notify = False


class TestGovCleanBumps:
    def _reset(self):
        for root in (state.L_ROOT, state.S_ROOT, state.CLOUD_L_ROOT):
            if root.exists():
                shutil.rmtree(root)
            root.mkdir(parents=True, exist_ok=True)
        lib._invalidate_lib_cache()
        storage.db_doc_delete(storage.DOC_WASH_RESIDUALS)
        governance._strategy = lambda: {
            'decision': 'quality_first', 'multi_season_protect': 'compare',
            'tie_keep_local': False, 'exempt_keywords': [], 'special_action': 'compare',
        }

    def _touch(self, root, rel):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('http://127.0.0.1/fake', encoding='utf-8')
        return p

    def test_real_inter_clean_bumps_library_and_plans(self, isolated_state):
        self._reset()
        d = '电影/华语电影/多版本电影 (2020)'
        self._touch(state.S_ROOT, f'{d}/多版本电影.720p.strm')
        self._touch(state.S_ROOT, f'{d}/多版本电影.1080p.strm')
        pid = governance.save_plan(governance.build_plan())
        assert pid

        before = sync.versions()
        res = governance.action_inter_clean(_Args(plan=pid))
        assert res['status'] == 'success', res
        after = sync.versions()
        assert after['library'] > before['library']
        assert after['plans'] > before['plans']
        assert after['records'] > before['records']


class TestSyncFrontend:
    def test_sync_block_exists(self):
        html = (_REPO_ROOT / 'static' / 'index.html').read_text(encoding='utf-8')
        assert '/api/sync/versions' in html
        assert 'document.hidden' in html
        assert 'visibilityState' in html
        assert 'TAB_DOMAINS' in html
        assert 'syncPoll' in html and 'reloadTab' in html
