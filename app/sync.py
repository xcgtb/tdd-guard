# -*- coding: utf-8 -*-
"""缓存失效总线（多端同步的唯一协调点）。

域（domain）= 一类可失效的数据；每个域在 kv 表里有一个单调递增的版本号 ``sync:<domain>``。
任何写操作 ``bump()`` 一个或多个域 → 版本号 +1 → 本进程执行该域注册的清理回调；
其它进程（如 CLI ``python -m app.engine``）写库后，Web 进程通过 ``check_external()``
发现「库里的版本号 > 本进程上次见到的」时补跑回调。

前端轮询 ``GET /api/sync/versions``（GET 不进 CSRF 中间件），拿到版本号后与本地比较，
只重载「当前可见页签关心」且版本有变化的域，实现多个浏览器/设备自动刷新。

本模块只依赖 state / storage，业务模块（stats / routers / config）在调用时导入本模块，
注册的清理回调里再用函数级导入取 stats / lib，避免 import 环（static_check 允许函数级 `from . import x`）。
"""
import logging
import threading

from . import state, storage

log = logging.getLogger('media_agent')

DOMAINS = ('library', 'explore', 'subscriptions', 'plans', 'governance', 'ingest', 'config', 'records')

# 语义蕴含：library 变了 explore 必然变；plans（治理单）变了 governance 概览必然变
IMPLIES = {'library': ('explore',), 'plans': ('governance',)}

_KV_PREFIX = 'sync:'

_CALLBACKS = {}          # domain -> [fn, ...]  缓存清理回调
_SEEN = {}               # domain -> 本进程已见到的版本号（防重复触发 / 自触发）
_LOCK = threading.RLock()


def register(domain, fn) -> None:
    """登记某域的缓存清理回调（模块导入时调用）。同一域可登记多个，按登记顺序执行。"""
    if domain not in DOMAINS:
        raise ValueError('未知同步域: %s' % domain)
    _CALLBACKS.setdefault(domain, []).append(fn)


def _expand(domains):
    """按 IMPLIES 展开并去重（保序），返回最终要 bump 的域列表。"""
    seen, out, stack = set(), [], list(domains)
    while stack:
        d = stack.pop(0)
        if d in seen:
            continue
        seen.add(d)
        out.append(d)
        stack.extend(IMPLIES.get(d, ()))
    return out


def _run(domain, reason=''):
    """执行某域全部回调；单个回调异常只记 warning，不影响其它回调。"""
    for fn in list(_CALLBACKS.get(domain, ())):
        try:
            fn()
        except Exception as e:
            log.warning('缓存失效回调执行失败 domain=%s reason=%s: %s: %s',
                        domain, reason, type(e).__name__, e)


def bump(*domains, invalidate=True, reason='') -> dict:
    """广播一次数据变更：展开 IMPLIES → 每域版本号 +1 → （可选）执行清理回调。

    返回本次各域的新版本号 {domain: int}。自增失败（库不可用）的域不返回、也不记 seen，
    下次 check_external 仍能发现外部变更。
    """
    expanded = _expand(domains)
    out = {}
    with _LOCK:
        for d in expanded:
            n = storage.db_kv_incr(_KV_PREFIX + d)
            if n <= 0:
                log.warning('缓存失效总线自增失败 domain=%s', d)
                continue
            _SEEN[d] = n          # 本进程自己触发的变更，视为已见，避免 check_external 重复触发
            out[d] = n
    if invalidate:
        for d in expanded:
            _run(d, reason)
    return out


def versions() -> dict:
    """一次读齐所有域的版本号 {domain: int}；缺失/损坏按 0。"""
    raw = storage.db_kv_many(_KV_PREFIX)
    out = {d: 0 for d in DOMAINS}
    for k, v in raw.items():
        d = k[len(_KV_PREFIX):]
        try:
            out[d] = int(v)
        except (TypeError, ValueError):
            out[d] = 0
    return out


def check_external() -> None:
    """发现「其它进程写库」造成的版本变化并补跑对应域的回调。

    首次调用（本进程还没记录过任何域）只登记版本号、不补跑回调——冷启动时缓存本就为空。
    """
    cur = versions()
    fired = []
    with _LOCK:
        for d, n in cur.items():
            if d not in _SEEN:
                _SEEN[d] = n      # 冷启动：只认领
                continue
            if n > _SEEN[d]:
                _SEEN[d] = n      # 先推进 seen，保证只补跑一次
                fired.append(d)
    for d in fired:
        _run(d, 'external')


# ═══════════════ 内置：library 域（文件/库数据变动）═══════════════
def _clear_library_caches():
    """清空所有与「媒体库文件」相关的内存缓存，并为磁盘(库)缓存打 stale 标记。

    磁盘缓存（emby 索引 / 片库映射总览）光清内存不够：读者会把旧库副本再塞回内存。
    state._stale 记录需要「立即后台重建」的磁盘缓存名，由读取方
    （tmdb.emby_library_index / morning.emby_library_overview）检查后触发刷新。
    """
    from . import stats, lib        # 函数级导入：避免 sync ↔ stats 模块级 import 环
    state._ep_cache['ts'] = 0
    state._ep_cache['data'] = None
    state._emby_index_cache['ts'] = 0
    state._emby_index_cache['data'] = None
    state._emby_lib_cache['ts'] = 0
    state._emby_lib_cache['data'] = None
    state._live_eps_cache.clear()
    state._stale.add('emby_index')
    state._stale.add('emby_overview')
    lib._invalidate_lib_cache()
    stats._clear_stats_caches()


register('library', _clear_library_caches)
