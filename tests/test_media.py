# -*- coding: utf-8 -*-
"""app/media.py 统一数据访问路径测试。

覆盖：
  - 幽灵规则 is_ghost：库根内但文件与目录都不在 → 幽灵；映射不到 / 库根不可用不误判；
  - ProviderIds 校验：假 Emby 无视 AnyProviderIdEquals 返回全部 Series 时只认匹配项；
  - 三个入口（订阅 latest_ep / 探索 live_eps / 片库映射 live_episodes）对同一份假
    Emby 数据给出一致的分集集合；
  - tmdb_aired_map 边界：无 last_episode_to_air、S00、未来季、零集季。
"""
import shutil

from app import emby, media, morning, state, subscribe, tmdb


def _setup_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(state, 'L_ROOT', tmp_path / 'local')
    monkeypatch.setattr(state, 'S_ROOT', tmp_path / 'share')
    monkeypatch.setattr(state, 'EMBY_PATHS', state.emby_path_map('/media/local', '/media/share'))


class TestGhostRule:
    def test_missing_file_and_dir_is_ghost(self, monkeypatch, tmp_path):
        local = tmp_path / 'local'
        local.mkdir()
        _setup_paths(monkeypatch, tmp_path)
        # 文件与目录都不存在 → 幽灵
        assert media.is_ghost('/media/local/剧集/剧/S01/x.S01E01.strm') is True

    def test_existing_dir_is_not_ghost(self, monkeypatch, tmp_path):
        local = tmp_path / 'local'
        (local / '剧集' / '剧' / 'S01').mkdir(parents=True)
        _setup_paths(monkeypatch, tmp_path)
        # 目录还在（分集可能被重新刮削）→ 不是幽灵
        assert media.is_ghost('/media/local/剧集/剧/S01/x.S01E01.strm') is False
        (local / '剧集' / '剧' / 'S01' / 'x.S01E01.strm').write_text('', encoding='utf-8')
        assert media.is_ghost('/media/local/剧集/剧/S01/x.S01E01.strm') is False
        # 整个季目录被 CD2 删掉 → 幽灵
        shutil.rmtree(local / '剧集')
        assert media.is_ghost('/media/local/剧集/剧/S01/x.S01E01.strm') is True

    def test_share_root_maps_to_share(self, monkeypatch, tmp_path):
        (tmp_path / 'share').mkdir()
        _setup_paths(monkeypatch, tmp_path)
        assert media.is_ghost('/media/share/剧集/剧/S01/x.strm') is True

    def test_unmappable_path_never_ghost(self, monkeypatch, tmp_path):
        _setup_paths(monkeypatch, tmp_path)
        assert media.is_ghost('/other/ghost/deleted.S01E02.strm') is False
        assert media.is_ghost('') is False

    def test_missing_library_root_disables_ghost_filter(self, monkeypatch, tmp_path):
        # NAS 掉挂载：库根不存在时不做幽灵过滤，避免整库误判成已删除
        _setup_paths(monkeypatch, tmp_path)
        assert media.is_ghost('/media/local/剧集/剧/S01/x.strm') is False


class TestProviderIdsVerification:
    def test_server_ignoring_filter_only_returns_match(self, monkeypatch):
        all_series = [
            {'Id': 'A', 'Name': 'A', 'ProviderIds': {'Tmdb': '111'}},
            {'Id': 'B', 'Name': 'B', 'ProviderIds': {'tmdb': '222'}},   # 小写键同样识别
            {'Id': 'C', 'Name': 'C', 'ProviderIds': {'Tmdb': '333'}},
            {'Id': 'D', 'Name': 'D'},                                    # 无 ProviderIds
        ]
        calls = []

        def fake(path, params=None, **kw):
            calls.append(params or {})
            if (params or {}).get('IncludeItemTypes') == 'Series':
                return {'Items': list(all_series)}      # 无视 AnyProviderIdEquals
            return {'Items': []}

        monkeypatch.setattr(emby, 'emby_request', fake)
        media.invalidate()
        assert media.series_ids_by_tmdb('222') == ['B']
        assert media.series_ids_by_tmdb('333') == ['C']
        # 不匹配时绝不把全库 Series 当成命中
        assert media.series_ids_by_tmdb('999') == []
        assert any(c.get('AnyProviderIdEquals') == 'Tmdb.999' for c in calls)

    def test_fallback_id_used_when_ids_cannot_be_verified(self, monkeypatch):
        def fake(path, params=None, **kw):
            if (params or {}).get('IncludeItemTypes') == 'Series':
                return {'Items': [{'Id': 'SR1', 'Name': '剧'}]}   # 无 ProviderIds
            return {'Items': []}

        monkeypatch.setattr(emby, 'emby_request', fake)
        media.invalidate()
        assert media.series_ids_by_tmdb('555') == []
        assert media.series_ids_by_tmdb('555', 'SR1') == ['SR1']


class TestEntryPointsConsistent:
    def test_same_episode_set_for_same_emby_data(self, monkeypatch, tmp_path):
        _setup_paths(monkeypatch, tmp_path)   # 库根不存在 → 关闭幽灵过滤，专测口径一致
        eps = [{'ParentIndexNumber': 1, 'IndexNumber': e,
                'Path': f'/media/local/剧集/剧/S01/x.S01E{e:02d}.strm'} for e in range(1, 6)]

        def fake(path, params=None, **kw):
            p = params or {}
            if p.get('IncludeItemTypes') == 'Series':
                return {'Items': [{'Id': 'SR1', 'Name': '剧', 'ProviderIds': {'Tmdb': '555'}}]}
            if p.get('IncludeItemTypes') == 'Episode':
                return {'Items': list(eps)}
            return {}

        monkeypatch.setattr(emby, 'emby_request', fake)
        media.invalidate()
        expected = {(1, e) for e in range(1, 6)}

        live = media.series_live('555', 'SR1', use_cache=False)
        assert live['episodes'] == expected and live['series_name'] == '剧'

        exp = tmdb._emby_series_live_eps('555', 'SR1', use_cache=False)
        assert exp['episodes'] == expected
        assert (exp['have_eps'], exp['local_eps'], exp['share_eps']) == (5, 5, 0)

        sub = subscribe._emby_series_latest_ep('555')
        assert sub['episodes'] == expected
        assert (sub['season'], sub['episode']) == (1, 5)

        morn = morning._live_series_episodes('SR1')
        assert {(e['ParentIndexNumber'], e['IndexNumber']) for e in morn} == expected


class TestTmdbAiredMap:
    def test_no_last_episode_to_air_uses_full_counts(self):
        info = {'seasons': [{'season_number': 0, 'episode_count': 5},
                            {'season_number': 1, 'episode_count': 10},
                            {'season_number': 2, 'episode_count': 8}]}
        assert media.tmdb_aired_map(info) == {1: 10, 2: 8}

    def test_last_episode_cuts_current_season(self):
        info = {'seasons': [{'season_number': 1, 'episode_count': 10},
                            {'season_number': 2, 'episode_count': 8}],
                'last_episode_to_air': {'season_number': 2, 'episode_number': 3}}
        assert media.tmdb_aired_map(info) == {1: 10, 2: 3}

    def test_future_season_ignored(self):
        info = {'seasons': [{'season_number': 1, 'episode_count': 8},
                            {'season_number': 2, 'episode_count': 8}],
                'last_episode_to_air': {'season_number': 1, 'episode_number': 8}}
        assert media.tmdb_aired_map(info) == {1: 8}

    def test_zero_count_and_special_excluded(self):
        info = {'seasons': [{'season_number': 0, 'episode_count': 9},
                            {'season_number': 1, 'episode_count': 0}]}
        assert media.tmdb_aired_map(info) == {}

    def test_empty_info(self):
        assert media.tmdb_aired_map(None) == {}
        assert media.tmdb_totals(None) == {'aired': 0, 'declared': 0}

    def test_totals_declared_includes_future_seasons(self):
        info = {'seasons': [{'season_number': 1, 'episode_count': 10},
                            {'season_number': 2, 'episode_count': 8}],
                'last_episode_to_air': {'season_number': 1, 'episode_number': 4}}
        assert media.tmdb_totals(info) == {'aired': 4, 'declared': 18}
