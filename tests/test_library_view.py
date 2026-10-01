# -*- coding: utf-8 -*-
"""
片库视图 / TMDB 剧集结构仓库 / 单剧实时同步 / 探索页 测试。

  - 仓库 TTL（完结 7 天 / 在更 12 小时 / 404 一天）、并发拉取、失败保留旧条目、Key 无效中止
  - library_view = 最新总览 ⊕ 仓库：总览变了（使徒行者补齐），卡片/弹窗口径立刻一致，不再「缺 67 集」
  - 单剧实时同步：补集后条目被修补；Emby 返回 0 集不删剧
  - 探索页：电影/剧集 id 不串；不每次读盘 tmdb_cache.json
"""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix='ttdguard_test_libview_'))
os.environ['L_ROOT'] = str(_TMP / 'local')
os.environ['S_ROOT'] = str(_TMP / 'share')
os.environ['CLOUD_L_ROOT'] = str(_TMP / 'cloud')
os.environ['AGENT_DATA'] = str(_TMP / 'data')
os.environ['TMDB_KEY'] = ''
os.environ['TG_BOT_TOKEN'] = ''
os.environ['INGEST_QUIET_MINUTES'] = '0'

sys.path.insert(0, str(Path(__file__).parent.parent / 'app'))
import engine  # noqa: E402
import pytest  # noqa: E402

_real_sleep = time.sleep
_real_alive = engine._alive_dir_map
LOCAL = engine.EMBY_PATHS.local
SHARE = engine.EMBY_PATHS.share


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, 'STATE_DIR', tmp_path)
    monkeypatch.setattr(engine, 'TMDB_META_FILE', tmp_path / 'tmdb_tv_meta.json')
    monkeypatch.setattr(engine, '_EMBY_OVERVIEW_CACHE_FILE', tmp_path / 'emby_overview_cache.json')
    monkeypatch.setattr(engine, 'EMBY_LIB_CACHE_FILE', tmp_path / 'emby_library_with_tmdb.json')
    monkeypatch.setitem(engine.RUNTIME_CFG, 'tmdb_key', 'k')
    monkeypatch.setitem(engine._emby_lib_cache, 'data', None)
    monkeypatch.setitem(engine._emby_lib_cache, 'ts', 0)
    monkeypatch.setitem(engine._tmdb_shared, 'path', None)
    monkeypatch.setattr(engine, '_kick_meta_fill', lambda ids: None)
    monkeypatch.setattr(time, 'sleep', lambda s: None)
    monkeypatch.setattr(engine, '_alive_dir_map', lambda paths: {})   # 目录核实另有专测，其余视为都存在
    yield tmp_path


def _eps(sid, spec):
    """spec: [(lib_root, season, [集号...])] → Emby 分集条目"""
    out = []
    for root, sn, ens in spec:
        for en in ens:
            out.append({'Id': f'{sid}-{sn}-{en}', 'ParentIndexNumber': sn, 'IndexNumber': en,
                        'Path': f'{root}/剧集/剧{sid}/Season {sn}/S{sn:02d}E{en:02d}.strm'})
    return out


def _entry(sid, tmdb_id, spec):
    e = {'id': sid, 'name': f'剧{sid}', 'tmdb_id': tmdb_id, 'path': f'{LOCAL}/剧集/剧{sid}'}
    engine._resync_series_entry(e, _eps(sid, spec))
    return e


def _set_overview(series, movies=()):
    engine._emby_lib_cache['data'] = {'series': list(series), 'movies': list(movies)}
    engine._emby_lib_cache['ts'] = time.time()


def _put_meta(entries):
    with engine._tmdb_meta_lock:
        engine._meta_load().update(entries)
        engine._tmdb_meta['dirty'] = True


class TestMetaStore:
    def test_ttl_and_force(self, monkeypatch):
        now = time.time()
        _put_meta({
            '1': {'ts': now - 3 * 86400, 'status': 'Ended', 'seasons': {'1': 10}},       # 完结 3 天：新鲜
            '2': {'ts': now - 8 * 86400, 'status': 'Canceled', 'seasons': {'1': 10}},    # 完结 8 天：过期
            '3': {'ts': now - 13 * 3600, 'status': 'Returning Series', 'seasons': {}},   # 在更 13h：过期
            '4': {'ts': now - 3600, 'status': 'Returning Series', 'seasons': {}},        # 在更 1h：新鲜
            '5': {'ts': now - 25 * 3600, 'missing': True},                               # 404 一天：过期
            '6': {'ts': now - 3600, 'missing': True},
        })
        fetched = []
        monkeypatch.setattr(engine, '_tmdb_http', lambda k, p, q: fetched.append(p) or
                            {'status': 'Ended', 'seasons': [{'season_number': 1, 'episode_count': 5}]})
        engine.tmdb_tv_meta(['1', '2', '3', '4', '5', '6', '7'])
        assert sorted(fetched) == ['/tv/2', '/tv/3', '/tv/5', '/tv/7']
        fetched.clear()
        engine.tmdb_tv_meta(['1', '4'], force_ids=['4'])
        assert fetched == ['/tv/4']
        fetched.clear()
        engine.tmdb_tv_meta(['3', '99'], stale_ok=True)   # 过期的先用旧的，只补缺失
        assert fetched == ['/tv/99']
        fetched.clear()
        engine.tmdb_tv_meta(['2'], fetch=False)
        assert fetched == []

    def test_concurrent_fetch_and_shape(self, monkeypatch, env):
        active, peak, lock = [0], [0], threading.Lock()

        def fake(key, path, params):
            with lock:
                active[0] += 1; peak[0] = max(peak[0], active[0])
            _real_sleep(0.03)
            with lock:
                active[0] -= 1
            if path == '/tv/404':
                return None
            return {'status': 'Returning Series', 'seasons': [
                {'season_number': 0, 'episode_count': 3}, {'season_number': 1, 'episode_count': 10},
                {'season_number': 2, 'episode_count': 0}]}
        monkeypatch.setattr(engine, '_tmdb_http', fake)
        ids = [str(i) for i in range(1, 25)] + ['404']
        seen = []
        out = engine.tmdb_tv_meta(ids, progress=lambda d, t: seen.append((d, t)))
        assert peak[0] > 1
        assert out['3']['seasons'] == {'1': 10} and out['3']['status'] == 'Returning Series'
        assert out['404'].get('missing') is True
        assert seen[0] == (0, 25) and seen[-1] == (25, 25)
        assert (env / 'tmdb_tv_meta.json').exists()      # 脏了才落盘，且已落盘

    def test_failure_keeps_old_and_auth_aborts(self, monkeypatch):
        old = {'ts': time.time() - 86400, 'status': 'Returning Series', 'seasons': {'1': 7}}
        _put_meta({'9': old})

        def boom(k, p, q):
            raise engine.TmdbError('网络挂了')
        monkeypatch.setattr(engine, '_tmdb_http', boom)
        errs = []
        out = engine.tmdb_tv_meta(['9'], errors=errs)
        assert out['9'] == old and errs == ['9']

        def deny(k, p, q):
            raise engine.TmdbAuthError('bad key')
        monkeypatch.setattr(engine, '_tmdb_http', deny)
        with pytest.raises(engine.TmdbError):
            engine.tmdb_tv_meta(['10', '11'])

    def test_no_write_when_clean(self, env):
        engine.tmdb_tv_meta(['1'], fetch=False)
        engine._meta_save()
        assert not (env / 'tmdb_tv_meta.json').exists()


class TestLibraryView:
    def test_shixingzhe_aligned_after_overview_refresh(self):
        """卡片/弹窗曾显示「缺 67 集 · 31/98」：总览已补齐到 98、仓库也是 98 → 必须对齐"""
        _put_meta({'7': {'ts': time.time(), 'status': 'Ended', 'seasons': {'1': 30, '2': 31, '3': 37}}})
        old = _entry('A', '7', [(LOCAL, 1, range(1, 32))])
        _set_overview([old])
        v = engine.library_view()
        assert v['series'][0]['tmdb_info']['match_status'] == 'missing'
        assert v['series'][0]['tmdb_info']['diff'] == -67
        # 总览刷新：三季齐全
        new = _entry('A', '7', [(SHARE, 1, range(1, 31)), (SHARE, 2, range(1, 32)), (SHARE, 3, range(1, 38))])
        _set_overview([new])
        v = engine.library_view()
        s = v['series'][0]
        assert s['tmdb_info']['match_status'] == 'aligned' and s['tmdb_info']['diff'] == 0
        assert s['have_eps'] == 98 and v['stats']['aligned'] == 1 and v['stats']['missing'] == 0
        assert 'tmdb_info' not in new            # 缓存里的原条目没被改
        assert v['ts'] == engine._emby_lib_cache['ts']

    def test_pending_no_tmdb_and_gap_report(self):
        _put_meta({'7': {'ts': time.time(), 'status': 'Ended', 'seasons': {'1': 10}}})
        _set_overview([_entry('A', '7', [(LOCAL, 1, range(1, 4))]),
                       _entry('B', '8', [(LOCAL, 1, range(1, 4))]),
                       _entry('C', None, [(LOCAL, 1, range(1, 4))])])
        v = engine.library_view()
        st = {s['id']: s['tmdb_info']['match_status'] for s in v['series']}
        assert st == {'A': 'missing', 'B': 'pending', 'C': 'no_tmdb'}
        rep = engine.gap_report()
        assert [x['id'] for x in rep['missing']] == ['A']

    def test_view_never_calls_tmdb(self, monkeypatch):
        monkeypatch.setattr(engine, '_tmdb_http', lambda *a: pytest.fail('不应访问 TMDB'))
        _set_overview([_entry('B', '8', [(LOCAL, 1, range(1, 4))])])
        assert engine.library_view()['series'][0]['tmdb_info']['match_status'] == 'pending'


class TestLiveSync:
    def _setup(self, monkeypatch, live):
        _put_meta({'7': {'ts': time.time(), 'status': 'Ended', 'seasons': {'1': 10}}})
        _set_overview([_entry('A', '7', [(LOCAL, 1, range(1, 4))])])
        monkeypatch.setattr(engine, 'emby_request', lambda path, params=None, **kw: live(params))

    def test_patch_when_changed(self, monkeypatch, env):
        def live(params):
            assert params['ParentId'] == 'A' and params['EnableImages'] == 'false'
            return {'Items': _eps('A', [(LOCAL, 1, range(1, 11))]), 'TotalRecordCount': 10}
        self._setup(monkeypatch, live)
        ts0 = engine._emby_lib_cache['ts']
        out = engine.live_sync_series(['A', ''])
        e = out['A']
        assert e['have_eps'] == 10 and e['tmdb_info']['match_status'] == 'aligned'
        assert engine._emby_lib_cache['data']['series'][0]['have_eps'] == 10   # 内存缓存已修补
        assert engine._emby_lib_cache['ts'] == ts0                           # 时间戳不动

    def test_dead_dir_episodes_dropped(self, monkeypatch):
        d1 = engine.L_ROOT / '剧集/剧A/Season 1'
        d1.mkdir(parents=True, exist_ok=True)            # 目录在但没有媒体文件 → 残留分集
        d2 = engine.L_ROOT / '剧集/剧A/Season 2'
        d2.mkdir(parents=True, exist_ok=True)
        (d2 / 'a.strm').write_text('x')                  # 仍有媒体文件 → 保留
        monkeypatch.setattr(engine, '_alive_dir_map', _real_alive)
        self._setup(monkeypatch, lambda p: {'Items': _eps('A', [(LOCAL, 1, range(1, 6)), (LOCAL, 2, [1])]),
                                            'TotalRecordCount': 6})
        assert [e['Id'] for e in engine._live_episodes_by_dir('A')] == ['A-2-1']

    def test_zero_episodes_does_not_remove(self, monkeypatch):
        self._setup(monkeypatch, lambda params: {'Items': [], 'TotalRecordCount': 0})
        out = engine.live_sync_series(['A'])
        assert out['A']['have_eps'] == 3
        assert len(engine._emby_lib_cache['data']['series']) == 1

    def test_emby_error_keeps_entry(self, monkeypatch):
        def live(params):
            raise RuntimeError('emby down')
        self._setup(monkeypatch, live)
        assert engine.live_sync_series(['A'])['A']['have_eps'] == 3

    def test_paginates(self, monkeypatch):
        pages = []

        def live(params):
            pages.append(params['StartIndex'])
            allx = _eps('A', [(LOCAL, 1, range(1, 3))]) * 1
            return {'Items': allx if params['StartIndex'] else [{'Id': 'x', 'ParentIndexNumber': 1, 'IndexNumber': 9, 'Path': ''}] * 5000,
                    'TotalRecordCount': 5002}
        self._setup(monkeypatch, live)
        assert len(engine._live_episodes_by_dir('A')) == 5002 and pages == [0, 5000]


class TestExplore:
    def _http(self, monkeypatch, calls):
        def fake(key, path, params):
            calls.append((path, params.get('page')))
            if path.startswith('/discover'):
                if params['page'] > 1:
                    return {'total_pages': 1, 'results': []}
                return {'total_pages': 1, 'total_results': 2, 'results': [
                    {'id': 100, 'name': 'X', 'title': 'X', 'poster_path': '/p.jpg'},
                    {'id': 200, 'name': 'Y', 'title': 'Y', 'poster_path': '/q.jpg'}]}
            if path.startswith('/tv/'):
                return {'status': 'Ended', 'seasons': [{'season_number': 1, 'episode_count': 12}]}
        monkeypatch.setattr(engine, '_tmdb_http', fake)

    def test_type_collision_and_total(self, monkeypatch):
        calls = []
        self._http(monkeypatch, calls)
        _set_overview([_entry('S100', '100', [(LOCAL, 1, range(1, 6))])],
                      [{'id': 'M200', 'tmdb_id': '200', 'in_share': True, 'in_local': False, 'has_image': True}])
        from argparse import Namespace
        tv = engine.action_explore(Namespace(media='tv', page=1))
        c = {x['tmdb_id']: x for x in tv['cards']}
        assert c['100']['in_emby'] and c['100']['emby_id'] == 'S100'
        assert c['100']['eps'] == {'local': 5, 'share': 0, 'have': 5, 'total': 12}
        assert not c['200']['in_emby']                      # 电影 200 不能让剧集 200 命中
        mv = engine.action_explore(Namespace(media='movie', page=1))
        c = {x['tmdb_id']: x for x in mv['cards']}
        assert c['200']['in_emby'] and c['200']['in_share'] and c['200']['emby_id'] == 'M200'
        assert not c['100']['in_emby'] and c['100']['eps'] is None

    def test_no_disk_reload_per_request(self, monkeypatch, env):
        calls = []
        self._http(monkeypatch, calls)
        _set_overview([])
        from argparse import Namespace
        engine.action_explore(Namespace(media='movie', page=1))
        n = len(calls)
        (env / 'tmdb_cache.json').write_text('{}', encoding='utf-8')   # 若每次读盘，缓存会被「清空」
        r = engine.action_explore(Namespace(media='movie', page=1))
        assert len(calls) == n and r['tmdb_hits'] >= 1
        assert r['status'] == 'success' and len(r['cards']) == 2

    def test_does_not_rebuild_overview_when_cached(self, monkeypatch, env):
        import json
        self._http(monkeypatch, [])
        # 只有过期的磁盘缓存：先返回旧数据，后台刷新（这里打桩），绝不同步重建
        (env / 'emby_overview_cache.json').write_text(
            json.dumps({'ts': time.time() - 3600, 'data': {'series': [], 'movies': []}}), encoding='utf-8')
        monkeypatch.setattr(engine, '_build_emby_library_overview', lambda: pytest.fail('不应同步重建'))
        monkeypatch.setattr(engine, '_overview_bg_refresh', lambda: None)
        from argparse import Namespace
        assert engine.action_explore(Namespace(media='tv', page=1))['status'] == 'success'
