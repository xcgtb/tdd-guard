# -*- coding: utf-8 -*-
"""
追更订阅（check_subscriptions）缺集 / 新集判定测试。

覆盖此前的误报场景：
  - 多季剧只拿「最新一集的集号」去比 TMDB 全部季总集数 → 每次都报缺几十集
  - 连载季 TMDB episode_count 含未播集 → 在更的剧永远「缺集」
  - 按 DateCreated 取最新集 → 洗版 / 重新入库的旧集让 latest_ep 倒退，下次又当新集播报
  - 同一集在本地 + 分享两库各一份 → 不能重复计数

Emby / TMDB 都用假实现替换，不联网；运行方式同 tests/test_engine.py。
"""
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix='ttdguard_subs_test_'))
os.environ['L_ROOT'] = str(_TMP / 'local')
os.environ['S_ROOT'] = str(_TMP / 'share')
os.environ['CLOUD_L_ROOT'] = str(_TMP / 'cloud')
os.environ['AGENT_DATA'] = str(_TMP / 'data')
os.environ['TMDB_KEY'] = ''
os.environ['TG_BOT_TOKEN'] = ''

sys.path.insert(0, str(Path(__file__).parent.parent / 'app'))
import engine  # noqa: E402

TMDB_ID = '9999'


def _ep(season, episode, created='2026-01-01T00:00:00Z', **extra):
    d = {'ParentIndexNumber': season, 'IndexNumber': episode, 'DateCreated': created}
    d.update(extra)
    return d


def _full(seasons: dict):
    """{季: 集数} → 每季 1..N 的 Emby 集列表"""
    return [_ep(s, e) for s, n in seasons.items() for e in range(1, n + 1)]


def _tmdb_info(seasons: dict, last=None, status='Returning Series'):
    """构造 /tv/{id} 响应；last=(季, 集) 对应 last_episode_to_air"""
    info = {
        'name': '测试剧', 'status': status,
        'seasons': [{'season_number': 0, 'episode_count': 5}] +
                   [{'season_number': s, 'episode_count': n, 'air_date': '2020-01-01'}
                    for s, n in seasons.items()],
    }
    if last:
        info['last_episode_to_air'] = {'season_number': last[0], 'episode_number': last[1]}
    return info


class _Env:
    """替换 emby_request / Tmdb / 订阅配置 / 状态文件，结束时全部还原"""

    def __init__(self):
        self.episodes = []
        self.series_items = [{'Id': 'S1', 'Name': '测试剧', 'ProviderIds': {'Tmdb': TMDB_ID}}]
        self.tmdb = None
        self.ep_calls = []

    def _emby(self, path, params=None, method='GET', timeout=15):
        params = params or {}
        if params.get('IncludeItemTypes') == 'Series':
            return {'Items': list(self.series_items)}
        if params.get('IncludeItemTypes') == 'Episode':
            self.ep_calls.append(dict(params))
            return {'Items': list(self.episodes)}
        return {}

    def __enter__(self):
        env = self

        class FakeTmdb:
            def get(self, path, ttl=0, **kw):
                return env.tmdb

            def save(self):
                pass

        self._orig = (engine.emby_request, engine.Tmdb, engine.SUB_STATE_FILE,
                      engine._cfg.get_subscriptions())
        self._orig_cfg = {k: engine._cfg.load_config().get(k)
                          for k in ('subscribe_enabled', 'subscribe_check_tmdb')}
        engine.emby_request = self._emby
        engine.Tmdb = FakeTmdb
        engine.STATE_DIR.mkdir(parents=True, exist_ok=True)
        engine.SUB_STATE_FILE = engine.STATE_DIR / 'subscriptions_state_test.json'
        if engine.SUB_STATE_FILE.exists():
            engine.SUB_STATE_FILE.unlink()
        cfg = engine._cfg.load_config()
        cfg['subscribe_enabled'] = '1'
        cfg['subscribe_check_tmdb'] = '1'
        engine._cfg.save_config(cfg)
        engine._cfg.set_subscriptions([{'id': 'sub1', 'tmdb_id': TMDB_ID, 'name': '测试剧', 'enabled': True}])
        return self

    def __exit__(self, *exc):
        if engine.SUB_STATE_FILE.exists():
            engine.SUB_STATE_FILE.unlink()
        engine.emby_request, engine.Tmdb, engine.SUB_STATE_FILE, subs = self._orig
        cfg = engine._cfg.load_config()
        for k, v in self._orig_cfg.items():
            if v is None: cfg.pop(k, None)
            else: cfg[k] = v
        engine._cfg.save_config(cfg)
        engine._cfg.set_subscriptions(subs)
        return False

    def run(self):
        r = engine.check_subscriptions(send_notify=False)
        return r['updates'][0] if r['updates'] else None

    def state(self):
        return engine._load_sub_state().get('sub1') or {}


class TestMissingEpisodes:
    def test_multi_season_complete_no_missing(self):
        with _Env() as env:
            env.episodes = _full({1: 10, 2: 10, 3: 10})
            env.tmdb = _tmdb_info({1: 10, 2: 10, 3: 10}, last=(3, 10), status='Ended')
            assert env.run() is None
            st = env.state()
            assert st['latest_ep'] == 'S03E10'
            assert st['tmdb_total'] == 30

    def test_ongoing_season_unaired_not_missing(self):
        # S03 共 10 集但只播到第 5 集，Emby 也有到 S03E05 → 不缺
        with _Env() as env:
            env.episodes = _full({1: 10, 2: 10, 3: 5})
            env.tmdb = _tmdb_info({1: 10, 2: 10, 3: 10}, last=(3, 5))
            assert env.run() is None
            assert env.state()['tmdb_total'] == 25

    def test_future_season_ignored(self):
        # TMDB 已公布 S04 但尚未开播
        with _Env() as env:
            env.episodes = _full({1: 8})
            env.tmdb = _tmdb_info({1: 8, 2: 8}, last=(1, 8))
            assert env.run() is None

    def test_gap_in_earlier_season_counted(self):
        with _Env() as env:
            eps = _full({1: 10, 2: 10, 3: 5})
            eps = [e for e in eps if (e['ParentIndexNumber'], e['IndexNumber']) not in ((1, 3), (2, 7))]
            env.episodes = eps
            env.tmdb = _tmdb_info({1: 10, 2: 10, 3: 10}, last=(3, 5))
            u = env.run()
            assert u is not None and u['new_ep'] is None
            assert u['missing'] == {'tmdb_total': 25, 'emby_total': 23, 'diff': 2}

    def test_extras_do_not_offset_gap(self):
        # Emby 多出一集未播集（TMDB 未计入）不能抵扣真实缺口；S00 特别篇也不计
        with _Env() as env:
            eps = [e for e in _full({1: 10}) if e['IndexNumber'] != 4]
            eps += [_ep(1, 11), _ep(0, 1), _ep(0, 2)]
            env.episodes = eps
            env.tmdb = _tmdb_info({1: 12}, last=(1, 10))
            u = env.run()
            assert u['missing']['diff'] == 1
            assert u['missing']['tmdb_total'] == 10

    def test_duplicates_across_libraries_not_double_counted(self):
        # 本地 + 分享各一份 S01E01-E05，缺 E06
        with _Env() as env:
            env.episodes = _full({1: 5}) + _full({1: 5})
            env.tmdb = _tmdb_info({1: 6}, last=(1, 6))
            u = env.run()
            assert u['missing'] == {'tmdb_total': 6, 'emby_total': 5, 'diff': 1}

    def test_multi_episode_file_covers_range(self):
        with _Env() as env:
            env.episodes = [_ep(1, 1, IndexNumberEnd=2), _ep(1, 3)]
            env.tmdb = _tmdb_info({1: 3}, last=(1, 3))
            assert env.run() is None

    def test_no_last_episode_to_air_fallback(self):
        # 没有 last_episode_to_air → 各季 episode_count 全部计入（旧口径）
        with _Env() as env:
            env.episodes = _full({1: 10, 2: 3})
            env.tmdb = _tmdb_info({1: 10, 2: 10})
            u = env.run()
            assert u['missing'] == {'tmdb_total': 20, 'emby_total': 13, 'diff': 7}

    def test_episode_query_fetches_all(self):
        with _Env() as env:
            env.episodes = _full({1: 3})
            env.tmdb = _tmdb_info({1: 3}, last=(1, 3))
            env.run()
            p = env.ep_calls[0]
            assert p['ParentId'] == 'S1' and int(p['Limit']) >= 5000
            assert 'SortBy' not in p

    def test_local_and_share_series_are_unioned(self):
        with _Env() as env:
            env.series_items = [
                {'Id': 'LOCAL', 'Name': '测试剧', 'ProviderIds': {'Tmdb': TMDB_ID}, 'Path': '/local/测试剧'},
                {'Id': 'SHARE', 'Name': '测试剧', 'ProviderIds': {'Tmdb': TMDB_ID}, 'Path': '/share/测试剧'},
            ]
            # Fake Emby endpoint uses the same episode set for both Series IDs only if we
            # explicitly vary it below.
            def fake_emby(path, params=None, method='GET', timeout=15):
                params = params or {}
                if params.get('IncludeItemTypes') == 'Series':
                    return {'Items': list(env.series_items)}
                if params.get('IncludeItemTypes') == 'Episode':
                    sid = params.get('ParentId')
                    return {'Items': _full({1: 5}) if sid == 'LOCAL' else _full({1: 6})}
                return {}
            old = engine.emby_request
            engine.emby_request = fake_emby
            try:
                env.tmdb = _tmdb_info({1: 6}, last=(1, 6), status='Ended')
                u = env.run()
                assert u is None
                assert env.state()['latest_ep'] == 'S01E06'
            finally:
                engine.emby_request = old


class TestNewEpisode:
    def test_readded_old_episode_not_announced(self):
        with _Env() as env:
            env.episodes = _full({1: 10, 2: 5})
            env.tmdb = _tmdb_info({1: 10, 2: 10}, last=(2, 5))
            env.run()
            assert env.state()['latest_ep'] == 'S02E05'
            # 洗版：S01E03 重新入库，DateCreated 最新
            env.episodes = [e for e in env.episodes if (e['ParentIndexNumber'], e['IndexNumber']) != (1, 3)]
            env.episodes.append(_ep(1, 3, created='2026-09-29T00:00:00Z'))
            assert env.run() is None
            assert env.state()['latest_ep'] == 'S02E05'

    def test_latest_never_moves_backwards(self):
        # 最新一集被删 / 下架，latest_ep 保持不退；再入库也不重复播报
        with _Env() as env:
            env.episodes = _full({1: 6})
            env.tmdb = _tmdb_info({1: 6}, last=(1, 6))
            env.run()
            env.episodes = _full({1: 5})
            u = env.run()
            assert u is None or u['new_ep'] is None
            assert env.state()['latest_ep'] == 'S01E06'
            env.episodes = _full({1: 6})
            assert env.run() is None

    def test_new_episode_announced(self):
        with _Env() as env:
            env.episodes = _full({1: 10, 2: 5})
            env.tmdb = _tmdb_info({1: 10, 2: 10}, last=(2, 5))
            assert env.run() is None
            env.episodes = _full({1: 10, 2: 6})
            env.tmdb = _tmdb_info({1: 10, 2: 10}, last=(2, 6))
            u = env.run()
            assert u['new_ep'] == {'old': 'S02E05', 'new': 'S02E06'}
            assert u['missing'] is None
            assert env.state()['latest_ep'] == 'S02E06'

    def test_new_season_announced(self):
        with _Env() as env:
            env.episodes = _full({1: 10})
            env.tmdb = _tmdb_info({1: 10, 2: 8}, last=(1, 10))
            env.run()
            env.episodes = _full({1: 10}) + [_ep(2, 1)]
            env.tmdb = _tmdb_info({1: 10, 2: 8}, last=(2, 1))
            u = env.run()
            assert u['new_ep'] == {'old': 'S01E10', 'new': 'S02E01'}
            assert u['missing'] is None
