# -*- coding: utf-8 -*-
"""
入库监控（engine 的 _fetch_ingest / refresh_ingest_cache / get_ingest / action_stats）。

覆盖大库场景下这次修复的几条硬要求：
  - 分页拉取：小页 + StartIndex，遇到早于 cutoff 的条目立即停止，不整库一次性拉
  - 失败不覆盖缓存、记录错误；有旧缓存照旧返回并标 stale，无缓存返回 status=error（不再 +0）
  - 单飞刷新：两线程并发只真正拉一次
  - Web 路径立即返回缓存/ pending，后台刷新不阻塞请求

沿用其它测试的做法：先把媒体目录/数据目录指到临时目录，再 import engine。
运行：`pytest tests/test_ingest.py -q`
"""
import datetime
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix='ttdguard_test_ingest_'))
os.environ.setdefault('L_ROOT', str(_TMP / 'local'))
os.environ.setdefault('S_ROOT', str(_TMP / 'share'))
os.environ.setdefault('CLOUD_L_ROOT', str(_TMP / 'cloud'))
os.environ.setdefault('AGENT_DATA', str(_TMP / 'data'))
os.environ.setdefault('TMDB_KEY', '')
os.environ.setdefault('TG_BOT_TOKEN', '')
os.environ.setdefault('INGEST_QUIET_MINUTES', '0')

sys.path.insert(0, str(Path(__file__).parent.parent / 'app'))
import engine  # noqa: E402


def _iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%S.0000000Z')


def _item(created, name='片A'):
    return {'Name': name, 'DateCreated': created,
            'Path': '/media/local/电影/' + name, 'SeriesName': name}


class _Env:
    """把 engine 的入库缓存与单飞状态整体隔离/复位，避免测试互相污染"""

    def __init__(self, tmp_path):
        self.cache_file = Path(tmp_path) / 'ingest_cache.json'
        engine.INGEST_CACHE_FILE = self.cache_file
        engine._INGEST_FETCH_LOCK = threading.Lock()
        engine._INGEST_STATE_LOCK = threading.Lock()
        engine._INGEST_SEQ = 0
        engine._INGEST_STATE = {'refreshing': False, 'error': None, 'error_ts': 0.0}
        engine._INGEST_LAST = {'data': None, 'ts': 0.0}

    def write_cache(self, ts, stats):
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        data = {'ts': ts, 'hours': 24, 'stats': stats,
                'tree': {'tv': {}, 'mov': {}}, 'movies_raw': [], 'episodes_raw': []}
        self.cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        return data


def _env(tmp_path):
    return _Env(tmp_path)


def _new_old_cutoff():
    now = datetime.datetime.now(datetime.timezone.utc)
    return _iso(now - datetime.timedelta(hours=1)), _iso(now - datetime.timedelta(hours=48))


# ─────────────────────── 1. 分页在 cutoff 处停止 ───────────────────────
def test_pagination_stops_at_cutoff(tmp_path, monkeypatch):
    _env(tmp_path)
    new, old = _new_old_cutoff()
    pages = {
        0: [_item(new, f'新{i}') for i in range(200)],      # 满页，全是新的
        200: [_item(new, '再新')] + [_item(old, '旧片')] + [_item(new, '尾')] * 198,
        400: [_item(new, '不该到这里')] * 200,
    }
    calls = []

    def fake(path, params=None, method='GET', timeout=15, body=None):
        assert path == '/Items'
        calls.append((params['IncludeItemTypes'], int(params['StartIndex']), params))
        return {'Items': pages.get(int(params['StartIndex']), [])}

    monkeypatch.setattr(engine, 'emby_request', fake)
    data = engine._fetch_ingest(hours=24)

    ep_calls = [(s, p) for t, s, p in calls if t == 'Episode']
    assert [s for s, _ in ep_calls] == [0, 200], '第 3 页（含旧条目后）不该再请求'
    p0 = ep_calls[0][1]
    assert p0['Limit'] == 200 and p0['EnableTotalRecordCount'] == 'false'
    assert p0['EnableImages'] == 'false' and p0['EnableUserData'] == 'false'
    assert p0['SortBy'] == 'DateCreated' and p0['SortOrder'] == 'Descending'
    # 200 条新的 + 第 2 页第一条新的（第二条旧条目处截断）；每个剧集名唯一
    assert data['stats']['episodes'] == 201
    assert data['stats']['series'] == 201
    assert len([c for c in calls if c[0] == 'Episode']) == 2


# ─────────────────── 2. 失败保留旧缓存并报告错误 ───────────────────
def test_failure_keeps_old_cache_and_reports_error(tmp_path, monkeypatch):
    env = _env(tmp_path)
    env.write_cache(ts=time.time() - 100000, stats={'movies': 5, 'series': 3, 'episodes': 40})

    def boom(hours=24):
        raise RuntimeError('Emby 超时')

    monkeypatch.setattr(engine, '_fetch_ingest', boom)

    out = engine.get_ingest(wait=True)
    assert out.get('stale') is True
    assert out.get('error') and '超时' in out['error']
    assert out['stats']['movies'] == 5, '失败时不能把统计清零'

    # 缓存文件没有被覆盖
    still = engine.read_ingest_cache()
    assert still['stats']['movies'] == 5

    # action_stats 也不返回 +0，并带上失败提示
    res = engine.action_stats(engine_action_args())
    assert res['status'] == 'success'
    assert res['stats']['movies'] == 5
    assert '扫描失败' in res['text']
    # ingest_status 上报错误与刷新状态
    st = engine.ingest_status()
    assert st['has_cache'] is True and st['refreshing'] is False
    assert st['error'] and '超时' in st['error']


# ─────────────── 3. 无缓存 + 失败 → error（不是 0） ───────────────
def test_no_cache_failure_returns_error_not_zeros(tmp_path, monkeypatch):
    env = _env(tmp_path)

    def boom(hours=24):
        raise RuntimeError('Emby 挂了')

    monkeypatch.setattr(engine, '_fetch_ingest', boom)

    out = engine.get_ingest(wait=True)
    assert out['status'] == 'error'
    assert '挂了' in out['message']
    assert out['stats'] == {}
    assert not env.cache_file.exists(), '失败不能写出缓存'

    res = engine.action_stats(engine_action_args(kw='full'))
    assert res['status'] == 'error'
    assert '失败' in res['text']

    st = engine.ingest_status()
    assert st['has_cache'] is False and st['error'] and '挂了' in st['error']


# ─────────────────────── 4. 单飞：只拉一次 ───────────────────────
def test_single_flight_two_threads_one_fetch(tmp_path, monkeypatch):
    _env(tmp_path)
    count = {'n': 0}
    lock = threading.Lock()

    def slow_fetch(hours=24):
        with lock:
            count['n'] += 1
        time.sleep(0.4)
        return {'ts': time.time(), 'hours': 24,
                'stats': {'movies': 1, 'series': 1, 'episodes': 1},
                'tree': {'tv': {}, 'mov': {}}, 'movies_raw': [], 'episodes_raw': []}

    monkeypatch.setattr(engine, '_fetch_ingest', slow_fetch)

    barrier = threading.Barrier(2)
    results = []

    def worker():
        barrier.wait()
        results.append(engine.refresh_ingest_cache())

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)

    assert count['n'] == 1, f'两线程并发应只拉一次，实际 {count["n"]} 次'
    assert len(results) == 2
    assert all(r['stats']['episodes'] == 1 for r in results)


# ───────────────── 5. Web 路径不阻塞，后台刷新 ─────────────────
def test_web_path_returns_immediately_while_refresh_runs(tmp_path, monkeypatch):
    _env(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def blocking_fetch(hours=24):
        started.set()
        release.wait(5)
        return {'ts': time.time(), 'hours': 24,
                'stats': {'movies': 2, 'series': 0, 'episodes': 0},
                'tree': {'tv': {}, 'mov': {}}, 'movies_raw': [], 'episodes_raw': []}

    monkeypatch.setattr(engine, '_fetch_ingest', blocking_fetch)

    t0 = time.time()
    out = engine.get_ingest(wait=False)          # 无缓存：应立即返回 pending
    elapsed = time.time() - t0
    assert elapsed < 1.0, f'Web 路径不应阻塞，耗时 {elapsed:.2f}s'
    assert out['status'] == 'pending' and out['refreshing'] is True

    assert started.wait(5), '后台刷新应已启动'
    # 刷新进行中，Web 再问一次仍然立即返回
    out2 = engine.get_ingest(wait=False)
    assert out2['status'] == 'pending' and out2['refreshing'] is True

    release.set()
    for _ in range(50):
        if engine._INGEST_LAST['data'] is not None:
            break
        time.sleep(0.1)
    out3 = engine.get_ingest(wait=False)
    assert out3.get('stats', {}).get('movies') == 2
    assert out3.get('refreshing') is False


# ─────────── 6. Web 读旧缓存立即返回，且触发后台刷新 ───────────
def test_web_returns_stale_cache_and_starts_refresh(tmp_path, monkeypatch):
    env = _env(tmp_path)
    env.write_cache(ts=time.time() - 700, stats={'movies': 9, 'series': 1, 'episodes': 2})
    started = threading.Event()
    release = threading.Event()

    def blocking_fetch(hours=24):
        started.set()
        release.wait(5)
        return {'ts': time.time(), 'hours': 24,
                'stats': {'movies': 0, 'series': 0, 'episodes': 0},
                'tree': {'tv': {}, 'mov': {}}, 'movies_raw': [], 'episodes_raw': []}

    monkeypatch.setattr(engine, '_fetch_ingest', blocking_fetch)

    t0 = time.time()
    out = engine.get_ingest(wait=False)
    assert time.time() - t0 < 1.0
    assert out['from_cache'] is True and out['stats']['movies'] == 9
    assert out['refreshing'] is True and started.wait(5)
    release.set()
    for _ in range(50):
        if engine._INGEST_LAST['data'] is not None:
            break
        time.sleep(0.1)


def test_web_refresh_skipped_when_cache_fresh(tmp_path, monkeypatch):
    env = _env(tmp_path)
    env.write_cache(ts=time.time() - 5, stats={'movies': 3, 'series': 0, 'episodes': 0})

    def should_not_run(hours=24):
        raise AssertionError('缓存新鲜时不该触发后台刷新')

    monkeypatch.setattr(engine, '_fetch_ingest', should_not_run)
    out = engine.get_ingest(wait=False)
    assert out['from_cache'] is True and out['refreshing'] is False
    assert out['stats']['movies'] == 3


def engine_action_args(kw=''):
    import argparse
    return argparse.Namespace(kw=kw)
