# -*- coding: utf-8 -*-
"""
影视探索 / 订阅缺集去重 / 晨报 / 晨报重试 的回归测试（v1.6.1）。

Emby / TMDB / Telegram 全部用假实现替换，不联网；运行方式同 tests/test_engine.py。
"""
import argparse
import datetime
import os
import sys
import tempfile
import time
import urllib.error
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix='ttdguard_explore_test_'))
os.environ['L_ROOT'] = str(_TMP / 'local')
os.environ['S_ROOT'] = str(_TMP / 'share')
os.environ['CLOUD_L_ROOT'] = str(_TMP / 'cloud')
os.environ['AGENT_DATA'] = str(_TMP / 'data')
os.environ['TMDB_KEY'] = ''
os.environ['TG_BOT_TOKEN'] = ''

sys.path.insert(0, str(Path(__file__).parent.parent / 'app'))
import engine  # noqa: E402
import test_subscriptions as ts  # noqa: E402  复用订阅测试的假环境

LOCAL = '/strm/115网盘/影视媒体库/'
SHARE = '/strm/115网盘/分享影视库/'


def _movie(i, tmdb, name='电影', path=LOCAL + '电影/a.strm'):
    return {'Id': i, 'Name': name, 'Type': 'Movie', 'Path': path, 'ProviderIds': {'Tmdb': tmdb}}


def _series(i, tmdb, name='剧集', path=SHARE + '剧/x'):
    return {'Id': i, 'Name': name, 'Type': 'Series', 'Path': path, 'ProviderIds': {'Tmdb': tmdb}}


class _EmbyIndexEnv:
    """替换 emby_request；movies / series 为 None 表示该类请求抛错"""

    def __init__(self, movies=(), series=()):
        self.movies, self.series, self.calls = movies, series, 0

    def _req(self, path, params=None, **kw):
        t = (params or {}).get('IncludeItemTypes')
        if t in ('Movie', 'Series'):
            self.calls += 1
            items = self.movies if t == 'Movie' else self.series
            if items is None:
                raise OSError('emby down')
            return {'Items': list(items)}
        return {'Items': [], 'TotalRecordCount': 0}

    def __enter__(self):
        self._orig = engine.emby_request
        engine.emby_request = self._req
        engine._emby_index_cache.update(ts=0, data=None, degraded=False)
        return self

    def __exit__(self, *exc):
        engine.emby_request = self._orig
        engine._emby_index_cache.update(ts=0, data=None, degraded=False)
        return False


class TestEmbyIndex:
    def test_movie_and_series_with_same_tmdb_id_do_not_collide(self):
        with _EmbyIndexEnv([_movie('m1', '1234', '某电影')], [_series('s1', '1234', '某剧')]):
            idx = engine.emby_library_index(force=True)
            assert idx['movie:1234']['name'] == '某电影'
            assert idx['tv:1234']['name'] == '某剧'
            assert len(idx) == 2

    def test_partial_failure_keeps_previous_entries_and_marks_degraded(self):
        env = _EmbyIndexEnv([_movie('m1', '1')], [_series('s1', '2')])
        with env:
            engine.emby_library_index(force=True)
            env.series = None                       # 剧集请求开始失败
            idx = engine.emby_library_index(force=True)
            assert 'tv:2' in idx, '剧集类请求失败时应沿用上一份缓存的剧集条目'
            assert 'movie:1' in idx
            assert engine.emby_index_degraded() is True

    def test_degraded_index_has_short_ttl(self):
        env = _EmbyIndexEnv([_movie('m1', '1')], None)
        with env:
            engine.emby_library_index(force=True)
            n = env.calls
            engine.emby_library_index()                       # 刚缓存：命中
            assert env.calls == n
            engine._emby_index_cache['ts'] = time.time() - 60  # 60 秒后：已超过 30 秒降级 TTL，必须重取
            engine.emby_library_index()
            assert env.calls > n

    def test_healthy_index_keeps_five_minute_ttl(self):
        env = _EmbyIndexEnv([_movie('m1', '1')], [_series('s1', '2')])
        with env:
            engine.emby_library_index(force=True)
            n = env.calls
            engine._emby_index_cache['ts'] = time.time() - 120
            engine.emby_library_index()
            assert env.calls == n
            assert engine.emby_index_degraded() is False


class _FakeTmdb:
    """discover 返回 total_pages 页；超出页码抛 TmdbError（模拟 TMDB 的 422）"""
    total_pages = 3

    def __init__(self):
        self.key = 'x'
        self.calls = self.hits = 0

    def get(self, path, ttl=0, **kw):
        if path.startswith('/tv/'):
            return {'seasons': [{'season_number': 1, 'episode_count': 10}]}
        pg = kw.get('page', 1)
        if pg > self.total_pages:
            raise engine.TmdbError('TMDB 返回 HTTP 422')
        return {'results': [{'id': 1000 + pg, 'title': 'T%d' % pg, 'release_date': '2020-01-01',
                             'vote_average': 7.0, 'poster_path': '/p.jpg'}],
                'total_pages': self.total_pages, 'total_results': self.total_pages}

    def save(self):
        pass


class TestExplore:
    def _run(self, media='movie', page=1, total_pages=3, movies=(), series=()):
        orig = engine.Tmdb
        _FakeTmdb.total_pages = total_pages
        engine.Tmdb = _FakeTmdb
        try:
            with _EmbyIndexEnv(movies, series):
                return engine.action_explore(argparse.Namespace(media=media, page=page))
        finally:
            engine.Tmdb = orig

    def test_total_pages_rounds_up(self):
        for tmdb_pages, expect in ((1, 1), (2, 1), (3, 2), (4, 2), (5, 3)):
            r = self._run(total_pages=tmdb_pages)
            assert r['status'] == 'success', r
            assert r['total_pages'] == expect, (tmdb_pages, r['total_pages'])

    def test_odd_total_last_page_does_not_fail(self):
        # TMDB 共 3 页：探索第 2 页要取 TMDB 第 3、4 页，第 4 页越界，应只显示第 3 页的内容
        r = self._run(page=2, total_pages=3)
        assert r['status'] == 'success', r
        assert [c['title'] for c in r['cards']] == ['T3']

    def test_first_page_failure_still_reports_error(self):
        r = self._run(page=3, total_pages=3)      # TMDB 第 5、6 页都越界
        assert r['status'] == 'error'

    def test_movie_card_not_marked_in_emby_by_series_with_same_id(self):
        # Emby 里只有「剧集 1001」，探索电影 1001 不应显示已入库
        r = self._run(media='movie', series=[_series('s1', '1001')])
        card = [c for c in r['cards'] if c['tmdb_id'] == '1001'][0]
        assert card['in_emby'] is False
        r = self._run(media='tv', series=[_series('s1', '1001')])
        card = [c for c in r['cards'] if c['tmdb_id'] == '1001'][0]
        assert card['in_emby'] is True and card['in_share'] is True

    def test_response_flags_degraded_emby(self):
        r = self._run(movies=None, series=[])
        assert r['status'] == 'success' and r['emby_degraded'] is True


class _FakeHttpErr(urllib.error.HTTPError):
    def __init__(self, code):
        super().__init__('http://x', code, 'err', {}, None)


class TestTmdb422:
    def test_422_returns_none_without_retry(self):
        calls = []

        def fake_urlopen(req, timeout=0):
            calls.append(1)
            raise _FakeHttpErr(422)
        orig, orig_sleep = engine.urllib.request.urlopen, engine.time.sleep
        engine.urllib.request.urlopen = fake_urlopen
        engine.time.sleep = lambda s: None
        try:
            t = engine.Tmdb()
            t.key = 'k'
            t.cache = {}
            assert t.get('/discover/movie', page=999) is None
            assert len(calls) == 1
        finally:
            engine.urllib.request.urlopen, engine.time.sleep = orig, orig_sleep


def _tmdb_with_air(seasons, last, air_date, status='Returning Series'):
    info = ts._tmdb_info(seasons, last=last, status=status)
    info['last_episode_to_air']['air_date'] = air_date
    return info


class TestSubscriptionGapDedupe:
    def _clean_log(self):
        try:
            engine.SUB_LOG_FILE.unlink()
        except OSError:
            pass

    def test_same_gap_not_reported_every_poll(self):
        self._clean_log()
        with ts._Env() as env:
            env.episodes = ts._full({1: 8})
            env.tmdb = ts._tmdb_info({1: 10}, last=(1, 10))
            u1 = env.run()
            assert u1 and u1['missing']['diff'] == 2
            assert env.run() is None, '同一批缺集第二轮不应再上报'
            assert env.run() is None

    def test_new_gap_reported_again(self):
        self._clean_log()
        with ts._Env() as env:
            env.episodes = ts._full({1: 8})
            env.tmdb = ts._tmdb_info({1: 10}, last=(1, 10))
            env.run()
            env.tmdb = ts._tmdb_info({1: 11}, last=(1, 11))   # 又播了一集，Emby 没跟上
            u = env.run()
            assert u and u['missing']['diff'] == 3

    def test_gap_filled_then_regression_reports_again(self):
        self._clean_log()
        with ts._Env() as env:
            env.episodes = ts._full({1: 8})
            env.tmdb = ts._tmdb_info({1: 10}, last=(1, 10))
            env.run()
            env.episodes = ts._full({1: 10})
            assert env.run() is None or env.run() is None
            assert env.state().get('gap_eps') == []
            env.episodes = ts._full({1: 8})                  # 又缺了
            u = env.run()
            assert u and u['missing']['diff'] == 2

    def test_filling_part_of_gap_is_not_a_new_report(self):
        self._clean_log()
        with ts._Env() as env:
            env.episodes = ts._full({1: 7})                  # 缺 E08-E10
            env.tmdb = ts._tmdb_info({1: 10}, last=(1, 10))
            env.run()
            env.episodes = ts._full({1: 8})                  # 补了 E08，仍缺 E09-E10：没有新缺口
            u = env.run()                                    # 会有一条「新集入库」，但不应再报缺集
            assert u is not None and u['new_ep'] and u['missing'] is None
            assert env.state()['gap_eps'] == ['S01E09', 'S01E10']
            assert env.run() is None

    def test_just_aired_latest_episode_gets_grace(self):
        self._clean_log()
        today = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')
        with ts._Env() as env:
            env.episodes = ts._full({1: 9})
            env.tmdb = _tmdb_with_air({1: 10}, (1, 10), today)
            assert env.run() is None, '今天刚播的最新一集还没入库不算缺集'

    def test_old_latest_episode_missing_is_reported(self):
        self._clean_log()
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=5)).strftime('%Y-%m-%d')
        with ts._Env() as env:
            env.episodes = ts._full({1: 9})
            env.tmdb = _tmdb_with_air({1: 10}, (1, 10), old)
            u = env.run()
            assert u and u['missing'] == {'tmdb_total': 10, 'emby_total': 9, 'diff': 1}

    def test_updates_are_journaled_even_when_not_notified(self):
        self._clean_log()
        with ts._Env() as env:
            env.episodes = ts._full({1: 5})
            env.tmdb = ts._tmdb_info({1: 5}, last=(1, 5))
            env.run()                                         # 建立基线
            env.episodes = ts._full({1: 6})
            env.tmdb = ts._tmdb_info({1: 6}, last=(1, 6))
            assert env.run()['new_ep'] == {'old': 'S01E05', 'new': 'S01E06'}
            assert env.run() is None                          # 状态已推进
            rows = engine.recent_sub_updates(24)
            assert len(rows) == 1 and rows[0]['new_ep']['new'] == 'S01E06'


class _MorningEnv:
    """晨报测试：假订阅配置 + 假通知；结束时还原"""

    def __init__(self):
        self.sent = []

    def __enter__(self):
        self._o = (engine._cfg.get_subscriptions, engine.notify_telegram,
                   engine.check_subscriptions, engine.action_emby_library,
                   engine._cfg.mark_morning_report_sent, engine.build_morning_report)
        self.marked = []
        engine._cfg.mark_morning_report_sent = lambda d: self.marked.append(d)
        engine._cfg.get_subscriptions = lambda: [{'id': 'a', 'tmdb_id': '1', 'name': 'A', 'enabled': True}]
        engine.notify_telegram = lambda text, chat_id=None: self.sent.append(text) or True
        engine._morning_pending.update(date='', parts=[], sent=0)
        return self

    def __exit__(self, *exc):
        (engine._cfg.get_subscriptions, engine.notify_telegram, engine.check_subscriptions,
         engine.action_emby_library, engine._cfg.mark_morning_report_sent,
         engine.build_morning_report) = self._o
        engine._morning_pending.update(date='', parts=[], sent=0)
        return False


def _boom(*a, **k):
    raise AssertionError('不应被调用')


def _write_gap_cache(age_sec):
    engine.save_emby_lib_cache({
        'status': 'success', 'ts': time.time() - age_sec, 'movies': [],
        'series': [{'id': 'S1', 'name': '缺集剧', 'tmdb_info': {'match_status': 'missing', 'diff': -3}}]},
        keep_ts=True)


class TestMorningReport:
    def test_subscription_section_reads_journal_and_does_not_rerun_check(self):
        with _MorningEnv():
            engine.check_subscriptions = _boom
            try:
                engine.SUB_LOG_FILE.unlink()
            except OSError:
                pass
            engine._append_sub_updates([{'name': '追更剧', 'tmdb_id': '1',
                                         'new_ep': {'old': 'S01E05', 'new': 'S01E06'}, 'missing': None}])
            text = engine.build_morning_report(['subscriptions'])
            assert '追更剧' in text and 'S01E06' in text
            assert '无变化' not in text

    def test_subscription_section_merges_same_series(self):
        rows = [
            {'ts': 1, 'name': 'A', 'tmdb_id': '1', 'new_ep': {'old': 'S01E01', 'new': 'S01E02'}, 'missing': None},
            {'ts': 2, 'name': 'A', 'tmdb_id': '1', 'new_ep': {'old': 'S01E02', 'new': 'S01E04'}, 'missing': None},
        ]
        m = engine._merge_sub_updates(rows, {})
        assert len(m) == 1 and m[0]['new_ep'] == {'old': 'S01E01', 'new': 'S01E04'}

    def test_resolved_gap_is_dropped_from_morning(self):
        rows = [{'ts': 1, 'name': 'A', 'tmdb_id': '1', 'new_ep': None,
                 'missing': {'tmdb_total': 10, 'emby_total': 8, 'diff': 2}}]
        assert engine._merge_sub_updates(rows, {'a': {'tmdb_id': '1', 'gap_eps': []}}) == []
        assert len(engine._merge_sub_updates(rows, {'a': {'tmdb_id': '1', 'gap_eps': ['S01E09']}})) == 1

    def test_gap_section_uses_stale_cache_and_never_scans(self):
        with _MorningEnv():
            engine.action_emby_library = _boom
            _write_gap_cache(age_sec=3 * 86400)
            text = engine.build_morning_report(['emby_gap'])
            assert '缺集剧' in text and '数据截至' in text

    def test_gap_section_without_cache_does_not_scan(self):
        with _MorningEnv():
            engine.action_emby_library = _boom
            try:
                engine.EMBY_LIB_CACHE_FILE.unlink()
            except OSError:
                pass
            text = engine.build_morning_report(['emby_gap'])
            assert '尚无' in text and '检查失败' not in text

    def test_gap_report_default_still_scans_when_no_cache(self):
        called = []
        with _MorningEnv():
            try:
                engine.EMBY_LIB_CACHE_FILE.unlink()
            except OSError:
                pass
            engine.action_emby_library = lambda a: called.append(1) or {'status': 'success', 'series': [], 'movies': []}
            r = engine.gap_report()
            assert called and r['status'] == 'success'


class TestMorningSendResume:
    LONG = '\n'.join('第 %03d 行 ' % i + '内容' * 40 for i in range(80))

    def test_retry_resumes_without_rebuild_or_duplicates(self):
        builds = []
        with _MorningEnv() as env:
            engine.build_morning_report = lambda items, force_refresh=False: builds.append(1) or self.LONG
            seq = iter([True, False, True, True, True, True])   # 第 2 段失败

            def notify(text, chat_id=None):
                ok = next(seq)
                if ok:
                    env.sent.append(text)
                return ok
            engine.notify_telegram = notify
            assert engine.send_morning_report([], resume=True) is False
            first_sent = list(env.sent)
            assert len(first_sent) == 1 and not env.marked
            assert engine.send_morning_report([], resume=True) is True
            assert len(builds) == 1, '重试不应重新生成晨报'
            assert env.sent[0] == first_sent[0] and len(set(env.sent)) == len(env.sent), '已发的段落不能重复发'
            assert '\n'.join(p.replace('\n</blockquote>', '') for p in env.sent).count('第 079 行') == 1
            assert env.marked

    def test_non_resume_rebuilds(self):
        builds = []
        with _MorningEnv():
            engine.build_morning_report = lambda items, force_refresh=False: builds.append(1) or '短'
            engine.send_morning_report([])
            engine.send_morning_report([])
            assert len(builds) == 2


class TestMorningSchedulerRetry:
    def _sched(self):
        from app import scheduler
        return scheduler

    def test_attempts_capped_with_backoff(self):
        sch = self._sched()
        calls = []
        orig = sch.engine.send_morning_report
        sch.engine.send_morning_report = lambda *a, **k: calls.append(1) or False
        sch._bg_state.update(morning_date='', morning_attempts=0, morning_next_try=0)
        try:
            now = 1000.0
            sch._morning_send_tick({'items': []}, '2026-10-01', now)
            sch._morning_send_tick({'items': []}, '2026-10-01', now + 30)    # 退避期内：不应再试
            assert len(calls) == 1
            for i in range(20):                                              # 不断推进时间
                now += 1000
                sch._morning_send_tick({'items': []}, '2026-10-01', now)
            assert len(calls) == sch.MORNING_MAX_ATTEMPTS
        finally:
            sch.engine.send_morning_report = orig

    def test_new_day_resets_attempts(self):
        sch = self._sched()
        calls = []
        orig = sch.engine.send_morning_report
        sch.engine.send_morning_report = lambda *a, **k: calls.append(1) or False
        sch._bg_state.update(morning_date='', morning_attempts=0, morning_next_try=0)
        try:
            now = 1000.0
            for _ in range(10):
                now += 1000
                sch._morning_send_tick({'items': []}, '2026-10-01', now)
            n1 = len(calls)
            sch._morning_send_tick({'items': []}, '2026-10-02', now + 1000)
            assert len(calls) == n1 + 1
        finally:
            sch.engine.send_morning_report = orig

    def test_success_passes_resume_flag(self):
        sch = self._sched()
        seen = {}
        orig = sch.engine.send_morning_report
        sch.engine.send_morning_report = lambda items, force_refresh=False, resume=False: seen.update(resume=resume) or True
        sch._bg_state.update(morning_date='', morning_attempts=0, morning_next_try=0)
        try:
            sch._morning_send_tick({'items': ['stats']}, '2026-10-01', 1000.0)
            assert seen == {'resume': True}
        finally:
            sch.engine.send_morning_report = orig
