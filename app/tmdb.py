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
from . import state, morning, emby, lib, storage, media

log = logging.getLogger('media_agent')


TMDB_BASE = os.environ.get('TMDB_BASE', 'https://api.themoviedb.org/3')
TMDB_LANG = os.environ.get('TMDB_LANG', 'zh-CN')
TMDB_IMG  = os.environ.get('TMDB_IMG', 'https://image.tmdb.org/t/p/w500')
TMDB_INFO_TTL = 24 * 3600
REGION_MAP = {
    'cn': {'with_origin_country': 'CN'}, 'hk': {'with_origin_country': 'HK'},
    'tw': {'with_origin_country': 'TW'}, 'jp': {'with_origin_country': 'JP'},
    'kr': {'with_origin_country': 'KR'}, 'us': {'with_origin_country': 'US'},
}
SORT_MAP = {
    'popularity': 'popularity.desc', 'release': 'primary_release_date.desc',
    'rating': 'vote_average.desc',
}
GENRE_MAP = {
    # 与 TMDB 官方 genre 列表对齐（movie: /genre/movie/list, tv: /genre/tv/list）
    '动作':   {'movie': 28,    'tv': 10759},
    '冒险':   {'movie': 12,    'tv': None},
    '喜剧':   {'movie': 35,    'tv': 35},
    '犯罪':   {'movie': 80,    'tv': 80},
    '纪录':   {'movie': 99,    'tv': 99},
    '剧情':   {'movie': 18,    'tv': 18},
    '家庭':   {'movie': 10751, 'tv': 10751},
    '奇幻':   {'movie': 14,    'tv': None},
    '历史':   {'movie': 36,    'tv': None},
    '恐怖':   {'movie': 27,    'tv': None},
    '音乐':   {'movie': 10402, 'tv': None},
    '悬疑':   {'movie': 9648,  'tv': 9648},
    '爱情':   {'movie': 10749, 'tv': None},
    '科幻':   {'movie': 878,   'tv': 10765},
    '惊悚':   {'movie': 53,    'tv': None},
    '战争':   {'movie': 10752, 'tv': 10768},
    '西部':   {'movie': 37,    'tv': 37},
    '动画':   {'movie': 16,    'tv': 16},
    '儿童':   {'movie': None,  'tv': 10762},
    '新闻':   {'movie': None,  'tv': 10763},
    '真人秀': {'movie': None,  'tv': 10764},
    '肥皂剧': {'movie': None,  'tv': 10766},
    '脱口秀': {'movie': None,  'tv': 10767},
    '电视电影': {'movie': 10770, 'tv': None},
}
_PRUNE_INTERVAL = 3600          # tmdb_cache 表过期清理：每小时最多一次
_last_prune = {'ts': 0.0}


class TmdbError(Exception):
    pass

class Tmdb:
    """TMDB 客户端。响应缓存两级：实例内存（本轮扫描）+ SQLite tmdb_cache 表（每个请求一行，
    请求成功立即落库——多个 Tmdb 实例并发也不会互相覆盖）。"""

    def __init__(self):
        self.key = (state.RUNTIME_CFG.get('tmdb_key') or '').strip()
        self.calls = self.hits = 0
        self.cache = {}

    def get(self, path, ttl=6 * 3600, **params):
        params.setdefault('language', TMDB_LANG)
        ck = path + '?' + urllib.parse.urlencode(sorted(params.items()))
        hit = self.cache.get(ck)
        if hit and time.time() - hit['ts'] < ttl:
            self.hits += 1
            return hit['data']
        cached = storage.db_tmdb_get(ck, ttl)
        if cached is not None:      # 库内命中不回填内存：内存里的 ts 会比库里新，绕过调用方更短的 ttl
            self.hits += 1
            return cached
        headers = {}
        if self.key.startswith('eyJ'):
            headers['Authorization'] = f'Bearer {self.key}'
        else:
            params['api_key'] = self.key
        url = f'{TMDB_BASE}{path}?{urllib.parse.urlencode(params)}'
        data = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as r:
                    data = json.loads(r.read().decode('utf-8'))
                break
            except urllib.error.HTTPError as e:
                if e.code in (401, 403): raise TmdbError(f'TMDB Key 无效或无权限 (HTTP {e.code})')
                if e.code == 429:
                    time.sleep(int(e.headers.get('Retry-After', '2')) + 1); continue
                if e.code == 404: return None
                if attempt == 2: raise TmdbError(f'TMDB 返回 HTTP {e.code}')
                time.sleep(1.5)
            except (urllib.error.URLError, OSError, ValueError) as e:
                if attempt == 2: raise TmdbError(f'无法访问 TMDB: {e}')
                time.sleep(1.5)
        if data is None: raise TmdbError('TMDB 多次请求失败')
        self.cache[ck] = {'ts': time.time(), 'data': data}
        storage.db_tmdb_put(ck, data)
        self.calls += 1
        time.sleep(0.03)
        return data

    def save(self):
        """兼容旧调用点：数据已在 get() 时逐条落库，这里只做过期清理（每小时最多一次，保留 24 小时）。"""
        now = time.time()
        if now - _last_prune['ts'] < _PRUNE_INTERVAL:
            return
        _last_prune['ts'] = now
        storage.db_tmdb_prune(storage.TMDB_MAX_AGE)

def _load_emby_index_disk():
    """探索页 Emby 索引持久缓存（SQLite 文档 emby_index，结构 {'ts', 'data'}）"""
    raw = storage.db_doc_get(storage.DOC_EMBY_INDEX)
    if isinstance(raw, dict) and isinstance(raw.get('data'), dict):
        try:
            return {'ts': float(raw.get('ts', 0)), 'data': raw['data']}
        except (TypeError, ValueError):
            pass
    return None

def _save_emby_index_disk(data):
    if not storage.db_doc_put(storage.DOC_EMBY_INDEX, {'ts': time.time(), 'data': data}):
        log.warning('Emby 探索索引缓存写入失败')

def _build_emby_library_index():
    out = {}
    disk_lookup = lib._disk_tmdb_lookup()  # 上游目录名 {tmdb-xxx} 兜底表
    for item_type in ('Movie', 'Series'):
        media_key = 'tv' if item_type == 'Series' else 'movie'
        try:
            for it in media.library_items(item_type):
                tmdb_id = media.tmdb_id_of(it, disk_lookup)
                if not tmdb_id: continue
                key = f'{media_key}:{tmdb_id}'
                path = it.get('Path', '') or ''
                lib_tag = state.emby_lib_of(path)
                row = out.setdefault(key, {
                    'id': it.get('Id'), 'ids': [], 'name': it.get('Name'), 'type': it.get('Type'),
                    'path': path, 'paths': [], 'year': it.get('ProductionYear'),
                    'rating': it.get('CommunityRating'), 'has_image': False, 'libs': set(),
                })
                if it.get('Id') and it.get('Id') not in row['ids']: row['ids'].append(it.get('Id'))
                if path and path not in row['paths']: row['paths'].append(path)
                if lib_tag: row['libs'].add(lib_tag)
                row['has_image'] = row['has_image'] or ('Primary' in (it.get('ImageTags') or {}))
                if not row.get('path') and path: row['path'] = path
        except Exception as e:
            log.warning('Emby 索引拉取失败 (%s): %s', item_type, e)
    for row in out.values():
        row['libs'] = sorted(row['libs'])
        row['in_local'] = 'local' in row['libs']
        row['in_share'] = 'share' in row['libs']
    return out

def _emby_index_bg_refresh():
    if not state._emby_index_refresh_lock.acquire(blocking=False): return
    try:
        out = _build_emby_library_index()
        state._emby_index_cache.update({'ts': time.time(), 'data': out})
        _save_emby_index_disk(out)
    except Exception as e:
        log.warning('Emby 探索索引后台刷新失败: %s', e)
    finally:
        state._emby_index_refresh_lock.release()

def emby_library_index(force=False):
    """探索页身份索引：内存 5 分钟 → 磁盘立即返回+后台刷新 → 首次同步构建。"""
    if not force:
        if state._emby_index_cache['data'] and time.time() - state._emby_index_cache['ts'] < 300:
            return state._emby_index_cache['data']
        disk = _load_emby_index_disk()
        if disk:
            state._emby_index_cache.update(disk)
            if time.time() - disk['ts'] >= 300:
                threading.Thread(target=_emby_index_bg_refresh, daemon=True,
                                 name='emby-index-refresh').start()
            return state._emby_index_cache['data']
    out = _build_emby_library_index()
    state._emby_index_cache.update({'ts': time.time(), 'data': out})
    _save_emby_index_disk(out)
    return out

def action_explore(args):
    region = getattr(args, 'region', 'all') or 'all'
    year = (getattr(args, 'year', '') or '').strip()
    sort = getattr(args, 'sort', 'popularity') or 'popularity'
    media = getattr(args, 'media', 'movie') or 'movie'
    page = int(getattr(args, 'page', 1) or 1)
    query = (getattr(args, 'q', '') or '').strip()
    genre = (getattr(args, 'genre', '') or '').strip()

    t = Tmdb()
    if not t.key:
        return {'status': 'error', 'message': '未配置 TMDB_KEY'}

    if query:
        # 搜索：media=all 走 /search/multi 一次搜到电影+剧集（结果带 media_type，可后切），
        # 明确选了 movie/tv 时仍用精确的分类型搜索。
        if media == 'all':
            tmdb_path = '/search/multi'
        else:
            tmdb_path = '/search/tv' if media == 'tv' else '/search/movie'
        params = {'query': query, 'page': page, 'include_adult': 'false'}
    else:
        # 浏览：TMDB discover 没有 all，媒体类型回落到 movie。
        if media == 'all':
            media = 'movie'
        tmdb_path = '/discover/tv' if media == 'tv' else '/discover/movie'
        params = {'page': page, 'sort_by': SORT_MAP.get(sort, 'popularity.desc'), 'vote_count.gte': 5}
        if region != 'all' and region in REGION_MAP: params.update(REGION_MAP[region])
        if year:
            params['first_air_date_year' if media == 'tv' else 'primary_release_year'] = year
        if genre and genre in GENRE_MAP:
            gid = GENRE_MAP[genre].get(media)
            if gid:
                params['with_genres'] = str(gid)

    # 一次请求 TMDB 2 页，合并为 40 张卡片
    try:
        params1 = dict(params); params1['page'] = page * 2 - 1
        params2 = dict(params); params2['page'] = page * 2
        res1 = t.get(tmdb_path, **params1) or {}
        res2 = t.get(tmdb_path, **params2) or {}
        results = (res1.get('results') or []) + (res2.get('results') or [])
        total_pages_raw = res1.get('total_pages') or 1
        res = {
            'results': results,
            'total_pages': max(1, total_pages_raw // 2),
            'total_results': res1.get('total_results', 0),
        }
    except TmdbError as e:
        return {'status': 'error', 'message': str(e)}

    emby_index = emby_library_index()
    # 探索页只读统一 TMDB 对照缓存，不为每张海报重新请求 /tv/{id}。
    # 注意：这份快照只用来兜底「本地/分享分库集数」与「TMDB 总集数」，
    # 剧集的实时集数由下面 _emby_series_live_eps 现查覆盖（见该函数注释）。
    eps_map = {}; tmdb_map = {}
    if media != 'movie':
        try:
            snap = morning.load_library_snapshot(max_age=1800) or morning.build_library_health_snapshot()
            for sr in snap.get('series', []):
                tid = str(sr.get('tmdb_id') or '')
                if not tid: continue
                eps_map[tid] = {'local_eps': int(sr.get('local_eps', 0) or 0),
                                 'share_eps': int(sr.get('share_eps', 0) or 0),
                                 'have_eps': int(sr.get('have_eps', 0) or 0)}
                tmdb_map[tid] = dict(sr.get('tmdb_info') or {})
        except Exception as e:
            log.warning('探索页统一快照读取失败: %s', e)

    page_items = (res.get('results') or [])[:40]

    # ── 剧集实时集数：并发现查 Emby，覆盖快照里的陈旧 have_eps ──
    # 快照有 30 分钟强制缓存，且刷新页面不会自愈；这里对每张「已入库」的剧集卡片
    # 直接问 Emby「这部剧现在有哪些集」，逐集与本地/分享路径分库统计。
    # 结果只覆盖集数，不改 in_emby / 海报：海报仍走索引缓存，避免多打一次请求。
    live_eps = {}
    live_total = {}
    if media != 'movie':
        targets = []
        for item in page_items:
            # multi 搜索结果里混着电影/剧集，只对剧集卡做实时集数
            if (item.get('media_type') or media) != 'tv':
                continue
            tid = str(item.get('id', ''))
            hit = emby_index.get('tv:' + tid)
            if hit is not None:
                targets.append((tid, hit.get('id')))
        if targets:
            def _one(target):
                tid, eid = target
                # TMDB 标称总集数：直接取 /tv/{id}（24h 缓存，命中即零成本），不再依赖
                # 30 分钟的统一快照，避免「Emby 是新的、TMDB 总数是旧的」造成 18/16 这类错位。
                try:
                    _info = t.get(f'/tv/{tid}', ttl=TMDB_INFO_TTL) or {}
                    _tot = sum(int(x.get('episode_count') or 0) for x in (_info.get('seasons') or [])
                               if (x.get('season_number') or 0) > 0)
                    if _tot > 0:
                        live_total[tid] = _tot
                except Exception as e:
                    log.warning('探索页 TMDB 总集数失败 %s: %s', tid, e)
                try:
                    return tid, _emby_series_live_eps(tid, eid)
                except Exception as e:
                    log.warning('探索页实时集数失败 %s: %s', tid, e)
                    return tid, None
            # 单剧 1~2 次请求，8 并发足以在百毫秒级跑完 40 部；用共享池避免每次新建线程
            try:
                with ThreadPoolExecutor(max_workers=8) as _ex:
                    for tid, res_live in _ex.map(_one, targets):
                        if res_live and not res_live.get('empty'):
                            live_eps[tid] = res_live
            except Exception as e:
                log.warning('探索页实时集数并发查询失败: %s', e)

    cards = []
    for item in page_items:
        # multi 搜索时每张卡的类型来自 media_type（'movie'/'tv'/'person'），discover 用全局 media
        item_media = item.get('media_type') or media
        if item_media == 'person':
            continue  # /search/multi 会返回人物，探索页不展示
        if item_media not in ('movie', 'tv'):
            item_media = media
        tmdb_id = str(item.get('id', ''))
        title = item.get('title') or item.get('name') or ''
        date_str = item.get('release_date') or item.get('first_air_date') or ''
        year_str = date_str[:4] if date_str else ''
        rating = item.get('vote_average') or 0
        poster = item.get('poster_path')
        emby_hit = emby_index.get(item_media + ':' + tmdb_id)
        in_emby = emby_hit is not None
        in_local = bool(emby_hit and emby_hit.get('in_local'))
        in_share = bool(emby_hit and emby_hit.get('in_share'))
        if poster: poster_url = f'{TMDB_IMG}{poster}'
        elif in_emby and emby_hit.get('has_image'): poster_url = f'/api/emby/poster/{emby_hit["id"]}'
        else: poster_url = ''

        # 入库进度（仅剧集）：实时集数优先，拿不到才退回统一快照
        eps = None
        eps_source = 'cache'
        if item_media == 'tv' and in_emby:
            e = eps_map.get(tmdb_id) or {'local_eps': 0, 'share_eps': 0, 'have_eps': 0}
            tmdb_info_cached = tmdb_map.get(tmdb_id) or {}
            # 展示口径与上游一致：优先 TMDB 标称总集数（如 18/24）；旧快照没有该字段时退回已播集数
            tmdb_total = int(live_total.get(tmdb_id) or tmdb_info_cached.get('declared_total') or tmdb_info_cached.get('tmdb_total') or 0)
            live = live_eps.get(tmdb_id)
            if live:
                e = {'local_eps': live['local_eps'], 'share_eps': live['share_eps'],
                     'have_eps': live['have_eps']}
                eps_source = 'live'
            eps = {
                'local': e['local_eps'], 'share': e['share_eps'],
                'have': e['have_eps'], 'total': tmdb_total,
            }

        cards.append({
            'tmdb_id': tmdb_id, 'title': title, 'year': year_str,
            'rating': round(rating, 1) if rating else None, 'poster': poster_url,
            'in_emby': in_emby, 'in_local': in_local, 'in_share': in_share,
            'emby_id': emby_hit['id'] if emby_hit else None,
            'type': item_media,
            'eps': eps, 'eps_source': eps_source if eps else None,
        })

    t.save()
    return {'status': 'success', 'page': page,
            'total_pages': min(res.get('total_pages', 1), 20),
            'total_results': res.get('total_results', 0),
            'cards': cards, 'is_search': bool(query),
            'tmdb_calls': t.calls, 'tmdb_hits': t.hits}

def classify_series_by_tmdb(local_seasons, tmdb_info):
    """按 TMDB 已播集数对照片库，避免连载未播集造成假缺集。"""
    local_map = {s['season']: s['episodes'] for s in local_seasons if s['season'] > 0}
    local_total = sum(local_map.values())
    if not tmdb_info:
        return {'match_status': 'unmatched', 'tmdb_status': None,
                'local_total': local_total, 'tmdb_total': None, 'declared_total': None, 'diff': None,
                'seasons': [{'season': sn, 'local': local_map[sn], 'tmdb': None, 'diff': None, 'status': 'unknown'} for sn in sorted(local_map)]}
    tmdb_status = tmdb_info.get('status', '') or ''
    # 已播集：唯一口径在 media.tmdb_aired_map（按 last_episode_to_air 裁剪、排除 S00）。
    tmdb_map = media.tmdb_aired_map(tmdb_info)
    # 标称总集数（含未播集，与上游 TgtoDrive 显示口径一致，如 18/24）；仅用于展示，缺集判断仍按已播集
    declared_total = media.tmdb_totals(tmdb_info)['declared']
    tmdb_total = sum(tmdb_map.values())
    season_diff = []
    for sn in sorted(set(local_map.keys()) | set(tmdb_map.keys())):
        l = local_map.get(sn, 0); t_ = tmdb_map.get(sn, 0)
        if l == t_: st = 'aligned'
        elif l < t_: st = 'missing'
        else: st = 'extra'
        season_diff.append({'season': sn, 'local': l, 'tmdb': t_, 'diff': l - t_, 'status': st})
    is_ongoing = tmdb_status in ('Returning Series', 'In Production', 'Planned', 'Pilot')
    if is_ongoing:
        status = 'aligned' if local_total >= tmdb_total else 'ongoing'
    else:
        if local_total == tmdb_total: status = 'aligned'
        elif local_total < tmdb_total: status = 'missing'
        else: status = 'extra'
    return {'match_status': status, 'tmdb_status': tmdb_status,
            'local_total': local_total, 'tmdb_total': tmdb_total, 'declared_total': declared_total,
            'diff': local_total - tmdb_total, 'seasons': season_diff}

def _emby_series_ids_by_tmdb(series_tmdb_id, fallback_id=None):
    """按 TMDB ID 找 Emby Series 条目 ID 列表（薄包装 media.series_ids_by_tmdb）。

    精确查询结果逐条校验 ProviderIds.Tmdb（服务端可能忽略 AnyProviderIdEquals 返回全库），
    再回退全量清单 / fallback_id 兜底。前身实现见 media._series_items_by_tmdb。
    """
    return media.series_ids_by_tmdb(series_tmdb_id, fallback_id)

def _emby_series_live_eps(series_tmdb_id, fallback_id=None, use_cache=True):
    """实时查某剧在 Emby 的分集集合（薄包装 media.series_live，不读任何快照）。

    返回 {'episodes': set[(季,集)], 'local_eps': int, 'share_eps': int,
          'have_eps': int, 'empty': bool}；完全查不到时返回 None。
    ``empty=True`` 表示「查到了 Series 但可归属到两个库根的分集为 0」——调用方应保留
    旧值而不是用 0 覆盖（避免 Emby 短暂异常时把卡片打成「未入库」）。
    """
    live = media.series_live(series_tmdb_id, fallback_id, use_cache=use_cache)
    if not live:
        return None
    local = live.get('local') or set()
    share = live.get('share') or set()
    have_n = len(local | share)
    return {'episodes': live.get('episodes') or set(), 'local_eps': len(local), 'share_eps': len(share),
            'have_eps': have_n, 'empty': have_n == 0}

def _tmdb_aired_set_from_info(info):
    """从已取得的 TMDB /tv/{id} 原始响应计算已播 (季,集)，不重复请求 TMDB。"""
    aired = set()
    for sn, n in media.tmdb_aired_map(info).items():
        aired.update((sn, e) for e in range(1, n + 1))
    return aired

def _tmdb_series_info(tmdb_id):
    """查 TMDB 已播集数、状态、季结构。
    已播集以 last_episode_to_air 为界：之前的季整季计入，最后播出的季只计到该集；
    连载季 episode_count 含未播集，直接求和会让在更的剧永远「缺集」。
    缺 last_episode_to_air 时退回旧口径（各季 episode_count 全部计入）。"""
    try:
        t = Tmdb()
        info = t.get(f'/tv/{tmdb_id}', ttl=TMDB_INFO_TTL)
        t.save()
        if not info: return None
        aired_seasons = {}
        for s in (info.get('seasons') or []):
            sn = s.get('season_number')
            if sn is None or sn <= 0: continue
            aired_seasons[sn] = {
                'episode_count': s.get('episode_count', 0) or 0,
                'air_date': s.get('air_date') or '',
            }
        last = info.get('last_episode_to_air') or {}
        aired = _tmdb_aired_set_from_info(info)
        totals = media.tmdb_totals(info)
        return {
            'name': info.get('name'),
            'status': info.get('status', ''),
            'last_episode_to_air': last,
            'seasons': aired_seasons,
            'total_episodes': totals['aired'],      # 已播集数（不含未播集 / S00）
            'declared_total': totals['declared'],   # 标称总集数（展示用）
            'aired': aired,                         # {(季, 集)}
        }
    except Exception as e:
        log.warning('TMDB 订阅查询失败 %s: %s', tmdb_id, e)
        return None
