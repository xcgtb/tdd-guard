# -*- coding: utf-8 -*-
"""从 engine.py 拆出。跨层符号统一经 _eng() 惰性访问（monkeypatch 穿透 + 双导入兼容）。"""
import os, re, sys, json, threading, shutil, fcntl, time, argparse, datetime, hashlib, logging, traceback
import urllib.parse, urllib.request, urllib.error
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import contextlib, dataclasses, html

log = logging.getLogger('media_agent')

try:
    from .core import (esc, parse_season_dir, get_ep, get_score, best_score, title_key,
                       governance_title_key, analyze_season_episodes, parse_emby_library,
                       quality_label, RE_SXXEXX)
except ImportError:
    from core import (esc, parse_season_dir, get_ep, get_score, best_score, title_key,
                      governance_title_key, analyze_season_episodes, parse_emby_library,
                      quality_label, RE_SXXEXX)


try:
    from . import config as _cfg
    from . import logger
except ImportError:
    import config as _cfg
    import logger


def _eng():
    try:
        from . import engine as _e
        return _e
    except ImportError:
        try:
            import engine as _e
            return _e
        except ImportError:
            return None


class TmdbError(Exception):
    pass

class Tmdb:
    def __init__(self):
        self.key = (_eng().RUNTIME_CFG.get('tmdb_key') or '').strip()
        self.cache_file = _eng().STATE_DIR / 'tmdb_cache.json'
        self.calls = self.hits = 0
        try:
            self.cache = json.loads(self.cache_file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            self.cache = {}

    def get(self, path, ttl=6 * 3600, **params):
        params.setdefault('language', _eng().TMDB_LANG)
        ck = path + '?' + urllib.parse.urlencode(sorted(params.items()))
        hit = self.cache.get(ck)
        if hit and time.time() - hit['ts'] < ttl:
            self.hits += 1
            return hit['data']
        headers = {}
        if self.key.startswith('eyJ'):
            headers['Authorization'] = f'Bearer {self.key}'
        else:
            params['api_key'] = self.key
        url = f'{_eng().TMDB_BASE}{path}?{urllib.parse.urlencode(params)}'
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
        self.calls += 1
        time.sleep(0.03)
        return data

    def save(self):
        try:
            _eng().STATE_DIR.mkdir(parents=True, exist_ok=True)
            cut = time.time() - 86400
            self.cache = {k: v for k, v in self.cache.items() if v['ts'] > cut}
            tmp = self.cache_file.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.cache, ensure_ascii=False), encoding='utf-8')
            tmp.replace(self.cache_file)
        except OSError as e:
            log.warning('TMDB 缓存写入失败: %s', e)

def _load_emby_index_disk():
    try:
        raw = json.loads(_eng()._EMBY_INDEX_CACHE_FILE.read_text(encoding='utf-8'))
        data = raw.get('data')
        if isinstance(data, dict): return {'ts': float(raw.get('ts', 0)), 'data': data}
    except (OSError, ValueError):
        pass
    return None

def _save_emby_index_disk(data):
    try:
        _eng().STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _eng()._EMBY_INDEX_CACHE_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps({'ts': time.time(), 'data': data}, ensure_ascii=False), encoding='utf-8')
        tmp.replace(_eng()._EMBY_INDEX_CACHE_FILE)
    except (OSError, TypeError) as e:
        log.warning('Emby 探索索引缓存写入失败: %s', e)

def _build_emby_library_index():
    out = {}
    disk_lookup = _eng()._disk_tmdb_lookup()  # 上游目录名 {tmdb-xxx} 兜底表
    for item_type in ('Movie', 'Series'):
        media_key = 'tv' if item_type == 'Series' else 'movie'
        try:
            data = _eng().emby_request('/Items', {
                'Recursive': 'true', 'IncludeItemTypes': item_type,
                'Fields': 'ProviderIds,Path,ProductionYear,CommunityRating,ImageTags',
                'Limit': 50000,
            }) or {}
            for it in data.get('Items', []):
                tmdb_id = str((it.get('ProviderIds') or {}).get('Tmdb') or '')
                if not tmdb_id:
                    _cp = _eng().emby_path_to_container(it.get('Path', '') or '')
                    tmdb_id = disk_lookup.get(str(_cp) if _cp else '', '')
                if not tmdb_id: continue
                key = f'{media_key}:{tmdb_id}'
                path = it.get('Path', '') or ''
                lib = _eng().emby_lib_of(path)
                row = out.setdefault(key, {
                    'id': it.get('Id'), 'ids': [], 'name': it.get('Name'), 'type': it.get('Type'),
                    'path': path, 'paths': [], 'year': it.get('ProductionYear'),
                    'rating': it.get('CommunityRating'), 'has_image': False, 'libs': set(),
                })
                if it.get('Id') and it.get('Id') not in row['ids']: row['ids'].append(it.get('Id'))
                if path and path not in row['paths']: row['paths'].append(path)
                if lib: row['libs'].add(lib)
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
    if not _eng()._emby_index_refresh_lock.acquire(blocking=False): return
    try:
        out = _build_emby_library_index()
        _eng()._emby_index_cache.update({'ts': time.time(), 'data': out})
        _save_emby_index_disk(out)
    except Exception as e:
        log.warning('Emby 探索索引后台刷新失败: %s', e)
    finally:
        _eng()._emby_index_refresh_lock.release()

def emby_library_index(force=False):
    """探索页身份索引：内存 5 分钟 → 磁盘立即返回+后台刷新 → 首次同步构建。"""
    if not force:
        if _eng()._emby_index_cache['data'] and time.time() - _eng()._emby_index_cache['ts'] < 300:
            return _eng()._emby_index_cache['data']
        disk = _load_emby_index_disk()
        if disk:
            _eng()._emby_index_cache.update(disk)
            if time.time() - disk['ts'] >= 300:
                threading.Thread(target=_emby_index_bg_refresh, daemon=True,
                                 name='emby-index-refresh').start()
            return _eng()._emby_index_cache['data']
    out = _build_emby_library_index()
    _eng()._emby_index_cache.update({'ts': time.time(), 'data': out})
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

    t = _eng().Tmdb()
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
        params = {'page': page, 'sort_by': _eng().SORT_MAP.get(sort, 'popularity.desc'), 'vote_count.gte': 5}
        if region != 'all' and region in _eng().REGION_MAP: params.update(_eng().REGION_MAP[region])
        if year:
            params['first_air_date_year' if media == 'tv' else 'primary_release_year'] = year
        if genre and genre in _eng().GENRE_MAP:
            gid = _eng().GENRE_MAP[genre].get(media)
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
            snap = _eng().load_library_snapshot(max_age=1800) or _eng().build_library_health_snapshot()
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
        if poster: poster_url = f'{_eng().TMDB_IMG}{poster}'
        elif in_emby and emby_hit.get('has_image'): poster_url = f'/api/emby/poster/{emby_hit["id"]}'
        else: poster_url = ''

        # 入库进度（仅剧集）：实时集数优先，拿不到才退回统一快照
        eps = None
        eps_source = 'cache'
        if item_media == 'tv' and in_emby:
            e = eps_map.get(tmdb_id) or {'local_eps': 0, 'share_eps': 0, 'have_eps': 0}
            tmdb_info_cached = tmdb_map.get(tmdb_id) or {}
            tmdb_total = int(tmdb_info_cached.get('tmdb_total') or 0)
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
                'local_total': local_total, 'tmdb_total': None, 'diff': None,
                'seasons': [{'season': sn, 'local': local_map[sn], 'tmdb': None, 'diff': None, 'status': 'unknown'} for sn in sorted(local_map)]}
    tmdb_status = tmdb_info.get('status', '') or ''
    tmdb_map = {}
    season_rows = tmdb_info.get('seasons') or []
    for ss in season_rows:
        sn = ss.get('season_number')
        if sn is None or sn <= 0:
            continue
        ec = int(ss.get('episode_count', 0) or 0)
        if ec <= 0:
            continue
        tmdb_map[int(sn)] = ec
    # 对于正在播出的最后一季，只统计 last_episode_to_air 之前已经播出的集；
    # 更后的季直接忽略。没有该字段时保留旧口径。
    last = tmdb_info.get('last_episode_to_air') or {}
    try:
        last_s = int(last.get('season_number') or 0)
        last_e = int(last.get('episode_number') or 0)
    except (TypeError, ValueError):
        last_s = last_e = 0
    if last_s > 0 and last_e > 0:
        tmdb_map = {sn: (last_e if sn == last_s else ec)
                    for sn, ec in tmdb_map.items()
                    if sn < last_s or sn == last_s}
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
            'local_total': local_total, 'tmdb_total': tmdb_total,
            'diff': local_total - tmdb_total, 'seasons': season_diff}

def _emby_series_ids_by_tmdb(series_tmdb_id, fallback_id=None):
    """按 TMDB ID 找 Emby Series 条目 ID 列表（实时，不读缓存）。

    优先级与 _emby_series_latest_ep 一致：① AnyProviderIdEquals 精确查询；
    ② 全量 Series 按 ProviderIds.Tmdb 过滤。两次都空时用 fallback_id 兜底
    （调用方从探索索引拿到的 Emby 条目 id，避免 ProviderIds 缺失时查不到）。
    """
    ids = []
    try:
        data = _eng().emby_request('/Items', {
            'Recursive': 'true', 'IncludeItemTypes': 'Series',
            'Fields': 'ProviderIds,Name', 'Limit': 50000,
            'AnyProviderIdEquals': f'Tmdb.{series_tmdb_id}',
        }) or {}
        ids = [it.get('Id') for it in (data.get('Items') or []) if it.get('Id')]
    except Exception as e:
        log.warning('实时集数：Series 精确查询失败 %s: %s', series_tmdb_id, e)
    if not ids:
        try:
            data = _eng().emby_request('/Items', {
                'Recursive': 'true', 'IncludeItemTypes': 'Series',
                'Fields': 'ProviderIds,Name', 'Limit': 50000,
            }) or {}
            ids = [it.get('Id') for it in (data.get('Items') or [])
                   if str((it.get('ProviderIds') or {}).get('Tmdb') or '') == str(series_tmdb_id)
                   and it.get('Id')]
        except Exception as e:
            log.warning('实时集数：Series 全量过滤失败 %s: %s', series_tmdb_id, e)
    if not ids and fallback_id:
        ids = [fallback_id]
    return ids

def _emby_series_live_eps(series_tmdb_id, fallback_id=None, use_cache=True):
    """实时查某剧在 Emby 的分集集合（不读任何快照）。

    返回 {'episodes': set[(季,集)], 'local_eps': int, 'share_eps': int,
          'have_eps': int, 'empty': bool}；完全查不到时返回 None。
    ``empty=True`` 表示「查到了 Series 但集数为 0」——调用方应保留旧值而不是
    用 0 覆盖（避免 Emby 短暂异常时把卡片打成「未入库」）。

    只统计 ``emby_lib_of(path)`` 能识别的分集：与 _build_emby_library_overview
    口径一致（只算落在本地/分享两个库根内的），顺带排除 CD2 已删源、Emby 还没
    刷掉的幽灵条目。
    """
    if not series_tmdb_id:
        return None
    ck = str(series_tmdb_id)
    if use_cache:
        with _eng()._live_eps_lock:
            hit = _eng()._live_eps_cache.get(ck)
            if hit and time.time() - hit[0] < _eng()._LIVE_EPS_TTL:
                return hit[1]

    ids = _emby_series_ids_by_tmdb(series_tmdb_id, fallback_id)
    if not ids:
        return None
    have, local_eps, share_eps = set(), set(), set()
    for sid in ids:
        try:
            data = _eng().emby_request('/Items', {
                'ParentId': sid, 'Recursive': 'true', 'IncludeItemTypes': 'Episode',
                'Fields': 'ParentIndexNumber,IndexNumber,IndexNumberEnd,Path',
                'Limit': 50000,
            }) or {}
        except Exception as e:
            log.warning('实时集数：拉取分集失败 series=%s: %s', sid, e)
            continue
        for ep in (data.get('Items') or []):
            try:
                sn = int(ep.get('ParentIndexNumber') or 0)
                en = int(ep.get('IndexNumber') or 0)
                en_end = int(ep.get('IndexNumberEnd') or en)
            except (TypeError, ValueError):
                continue
            if sn <= 0 or en <= 0:
                continue
            lib = _eng().emby_lib_of(ep.get('Path') or '')
            if not lib:
                continue  # 不在两个库根内：不算入库（与片库映射口径一致）
            for e in range(en, max(en, en_end) + 1):
                have.add((sn, e))
                (local_eps if lib == 'local' else share_eps).add((sn, e))

    out = {'episodes': have, 'local_eps': len(local_eps), 'share_eps': len(share_eps),
           'have_eps': len(have), 'empty': not have}
    if use_cache:
        with _eng()._live_eps_lock:
            _eng()._live_eps_cache[ck] = (time.time(), out)
            # 轻量清理：只保留最近 2000 条，避免长跑进程内存无界增长
            if len(_eng()._live_eps_cache) > 2000:
                for k, _v in sorted(_eng()._live_eps_cache.items(), key=lambda kv: kv[1][0])[:500]:
                    _eng()._live_eps_cache.pop(k, None)
    return out

def _tmdb_aired_set_from_info(info):
    """从已取得的 TMDB /tv/{id} 原始响应计算已播 (季,集)，不重复请求 TMDB。"""
    if not info:
        return set()
    aired_seasons = {}
    for ss in (info.get('seasons') or []):
        sn = ss.get('season_number')
        if sn is None or sn <= 0:
            continue
        try:
            aired_seasons[int(sn)] = int(ss.get('episode_count', 0) or 0)
        except (TypeError, ValueError):
            continue
    last = info.get('last_episode_to_air') or {}
    try:
        last_s = int(last.get('season_number') or 0)
        last_e = int(last.get('episode_number') or 0)
    except (TypeError, ValueError):
        last_s = last_e = 0
    aired = set()
    for sn, ec in aired_seasons.items():
        if last_s > 0 and last_e > 0:
            if sn < last_s: n = ec
            elif sn == last_s: n = last_e
            else: continue
        else:
            n = ec
        aired.update((sn, e) for e in range(1, n + 1))
    return aired

def _tmdb_series_info(tmdb_id):
    """查 TMDB 已播集数、状态、季结构。
    已播集以 last_episode_to_air 为界：之前的季整季计入，最后播出的季只计到该集；
    连载季 episode_count 含未播集，直接求和会让在更的剧永远「缺集」。
    缺 last_episode_to_air 时退回旧口径（各季 episode_count 全部计入）。"""
    try:
        t = _eng().Tmdb()
        info = t.get(f'/tv/{tmdb_id}', ttl=_eng().TMDB_INFO_TTL)
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
        return {
            'name': info.get('name'),
            'status': info.get('status', ''),
            'last_episode_to_air': last,
            'seasons': aired_seasons,
            'total_episodes': len(aired),   # 已播集数（不含未播集 / S00）
            'aired': aired,                 # {(季, 集)}
        }
    except Exception as e:
        log.warning('TMDB 订阅查询失败 %s: %s', tmdb_id, e)
        return None
