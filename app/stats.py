# -*- coding: utf-8 -*-
"""从 engine.py 拆出。共享状态在 state，跨模块符号按所属模块 `模块.名字` 在调用时访问（monkeypatch 所属模块即可穿透）。"""
import os, re, sys, json, threading, shutil, fcntl, time, argparse, datetime, hashlib, logging, traceback
import urllib.parse, urllib.request, urllib.error
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import contextlib, dataclasses, html

from . import config as _cfg
from . import logger
from .core import (esc, parse_season_dir, get_ep, title_key, governance_title_key,
                   analyze_season_episodes, parse_emby_library, quality_label, RE_SXXEXX)
from . import state, lib, storage

log = logging.getLogger('media_agent')


def _recompute_all_stats():
    """单次遍历双库，同时更新「分类统计」与「STRM 计数」两套缓存。
    之前两处各自独立遍历，时间不同/网络挂载抖动会导致数字对不上；
    统一从同一次遍历取数，保证总览卡片与总库分类统计永远一致。"""
    CATS = [
        ('\U0001F476 儿童节目', ['儿童']),
        ('\U0001F3A4 演唱会',       ['演唱会']),
        ('\U0001F3AA 综艺剧',       ['综艺']),
        ('\u26e9\ufe0f 动漫剧',     ['动漫']),
        ('\U0001F9F8 动画电影',   ['动画电影']),
        ('\U0001F4FD\ufe0f 纪录片',  ['纪录']),
        ('\U0001F1E8\U0001F1F3 国产剧', ['国产剧集', '国产剧']),
        ('\U0001F1FA\U0001F1F8 欧美剧', ['欧美剧集', '欧美剧', '其他剧集']),
        ('\U0001F1EF\U0001F1F5 日韩剧', ['日韩剧集', '日韩剧']),
        ('\U0001F1E8\U0001F1F3 华语电影', ['国产电影', '华语电影']),
        ('\U0001F310 外语电影', ['欧美电影', '日韩电影', '外语电影', '其他电影']),
    ]
    cat_names = [c[0] for c in CATS]

    out = {
        'local': {c: 0 for c in cat_names},
        'share': {c: 0 for c in cat_names},
        'local_other': 0,
        'share_other': 0,
        'local_total': 0,
        'share_total': 0,
    }

    for root, key in ((state.L_ROOT, 'local'), (state.S_ROOT, 'share')):
        if not root.exists():
            continue
        for f in root.rglob('*.strm'):
            try:
                parts = f.relative_to(root).parts
            except ValueError:
                continue
            matched = None
            for part in parts:
                for cat_name, kws in CATS:
                    hit = False
                    for kw in kws:
                        if kw in part:
                            hit = True
                            break
                    if hit:
                        matched = cat_name
                        break
                if matched:
                    break
            if matched:
                out[key][matched] += 1
            else:
                out[key + '_other'] += 1
            out[key + '_total'] += 1

    rows = []
    for cat in cat_names:
        rows.append({
            'name': cat,
            'local': out['local'][cat],
            'share': out['share'][cat],
            'total': out['local'][cat] + out['share'][cat],
        })
    result = {
        'status': 'success',
        'rows': rows,
        'local_total': out['local_total'],
        'share_total': out['share_total'],
        'local_other': out['local_other'],
        'share_other': out['share_other'],
    }
    now = time.time()
    state._lib_stats_cache['ts'] = now
    state._lib_stats_cache['data'] = result
    # 同一次遍历的总数同步写入 STRM 计数缓存（内存+磁盘），两处数字永远一致
    state._strm_count_cache.update({'ts': now, 'local': out['local_total'], 'share': out['share_total']})
    _save_strm_count_disk(out['local_total'], out['share_total'])
    return result

def action_library_stats(args):
    # 缓存命中则直接返回
    now = time.time()
    if state._lib_stats_cache['data'] is not None and (now - state._lib_stats_cache['ts']) < state._CACHE_TTL:
        return state._lib_stats_cache['data']
    return _recompute_all_stats()

def _load_strm_count_disk():
    """STRM 计数持久缓存（SQLite 文档 strm_count）：(local, share, ts) 或 None"""
    data = storage.db_doc_get(storage.DOC_STRM_COUNT)
    try:
        return int(data.get('local', 0)), int(data.get('share', 0)), float(data.get('ts', 0))
    except (AttributeError, TypeError, ValueError):
        return None

def _save_strm_count_disk(local, share):
    if not storage.db_doc_put(storage.DOC_STRM_COUNT, {'ts': time.time(), 'local': local, 'share': share}):
        log.warning('STRM 计数缓存写入失败')

def _strm_count_bg_refresh():
    """后台重算统计（单飞）。走 _recompute_all_stats 统一遍历，
    同时更新分类统计与 STRM 计数，保证两处数字一致。"""
    if not state._strm_count_refreshing.acquire(blocking=False):
        return
    try:
        _recompute_all_stats()
        log.info('后台刷新统计完成: local=%d share=%d',
                 state._strm_count_cache['local'], state._strm_count_cache['share'])
    except Exception as e:
        log.warning('统计后台刷新失败: %s', e)
    finally:
        state._strm_count_refreshing.release()

def _get_strm_counts():
    """STRM 总数三级缓存：内存(5分钟) → 磁盘(立即返回+后台刷新) → 同步首算。
    大库 rglob 全量遍历很慢，磁盘缓存保证总览页秒开，后台静默刷新。"""
    now = time.time()
    if now - state._strm_count_cache['ts'] < state._CACHE_TTL and state._strm_count_cache['ts'] > 0:
        return state._strm_count_cache['local'], state._strm_count_cache['share']
    disk = _load_strm_count_disk()
    if disk is not None:
        l, s_, ts = disk
        state._strm_count_cache.update({'ts': ts or (now - state._CACHE_TTL), 'local': l, 'share': s_})
        if time.time() - state._strm_count_cache['ts'] >= state._CACHE_TTL:
            threading.Thread(target=_strm_count_bg_refresh, daemon=True,
                             name='strm-count-refresh').start()
        return state._strm_count_cache['local'], state._strm_count_cache['share']
    # 首次无任何缓存：同步算一次并落盘
    l = sum(1 for _ in state.L_ROOT.rglob('*.strm')) if state.L_ROOT.exists() else 0
    s_ = sum(1 for _ in state.S_ROOT.rglob('*.strm')) if state.S_ROOT.exists() else 0
    state._strm_count_cache.update({'ts': now, 'local': l, 'share': s_})
    _save_strm_count_disk(l, s_)
    log.info('缓存刷新 STRM 计数: local=%d share=%d', l, s_)
    return l, s_

def invalidate_stats_cache():
    """清空统计缓存（删除后调用）"""
    state._strm_count_cache['ts'] = 0
    state._lib_stats_cache['ts'] = 0
    state._lib_stats_cache['data'] = None

def invalidate_media_caches(keep_emby_lib=False):
    """文件变动后统一失效内存缓存（分集 / Emby 索引 / lib.Lib 快照 / 统计）。
    keep_emby_lib=True 时保留片库映射缓存（单剧删除会就地修补它，避免整页重跑 TMDB 对照）。"""
    state._ep_cache['ts'] = 0
    state._ep_cache['data'] = None
    state._emby_index_cache['ts'] = 0
    state._emby_index_cache['data'] = None
    if not keep_emby_lib:
        state._emby_lib_cache['ts'] = 0
        state._emby_lib_cache['data'] = None
    lib._invalidate_lib_cache()
    invalidate_stats_cache()
