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
from . import state

log = logging.getLogger('media_agent')


# ═══════════════════ Emby ═══════════════════
EMBY_TIMEOUT = int(os.environ.get('EMBY_TIMEOUT', '30') or 30)   # 秒；大库 / NAS 繁忙时 15 秒容易超时
EMBY_RETRIES = 2                                                  # 仅 GET 在超时 / 5xx / 连接错误时重试
_MEDIA_EXT = {'.strm', '.mkv', '.mp4', '.ts', '.m2ts', '.avi', '.mov', '.wmv', '.flv', '.rmvb', '.iso'}


def emby_request(path, params=None, method='GET', timeout=None, body=None, retries=None):
    if not state.EMBY_KEY:
        raise RuntimeError('未配置 EMBY_KEY')
    timeout = timeout or EMBY_TIMEOUT
    url = state.EMBY_HOST + path + ('?' + urllib.parse.urlencode(params) if params else '')
    headers = {'X-Emby-Token': state.EMBY_KEY}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    else:
        data = b'' if method == 'POST' else None
    attempts = 1 + ((EMBY_RETRIES if retries is None else retries) if method == 'GET' else 0)
    for i in range(attempts):
        req = urllib.request.Request(url, method=method, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            break
        except urllib.error.HTTPError as e:
            if e.code < 500 or i == attempts - 1:
                raise
            err = e
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            if i == attempts - 1:
                raise
            err = e
        log.warning('Emby 请求失败(%s)，第 %d/%d 次重试: %s', path, i + 1, attempts - 1, err)
        time.sleep(1 + i)
    return json.loads(raw.decode('utf-8')) if (method == 'GET' and raw) else None

def container_to_emby_path(p, target):
    """容器内路径 → Emby 侧路径（notify_emby_deleted 用）；映射不上返回 None"""
    root = state.L_ROOT if target == 'local' else state.S_ROOT
    eroot = state.EMBY_PATHS.local if target == 'local' else state.EMBY_PATHS.share
    if not eroot:
        return None
    try:
        rel = Path(p).relative_to(root)
    except ValueError:
        return None
    return eroot.rstrip('/') + ('/' + rel.as_posix() if rel.parts else '')

def notify_emby_deleted(emby_paths, background=True):
    """告诉 Emby「这些路径已删除」（POST /Library/Media/Updated, UpdateType=Deleted）。
    比 /Library/Refresh 整库扫描快得多，Emby 会直接把对应条目（含没有分集的空剧）移除。
    失败时退回整库刷新。默认放后台线程，不阻塞删除接口。"""
    paths = sorted({p for p in (emby_paths or []) if p})

    def _run():
        if paths:
            for i in range(0, len(paths), 200):
                body = {'Updates': [{'Path': p, 'UpdateType': 'Deleted'} for p in paths[i:i + 200]]}
                ok = False
                for ep in ('/Library/Media/Updated', '/emby/Library/Media/Updated'):
                    try:
                        emby_request(ep, method='POST', timeout=15, body=body)
                        ok = True
                        break
                    except Exception as e:
                        log.warning('Emby 删除通知失败 %s: %s', ep, e)
                if not ok:
                    notify_emby_refresh()
                    return
        else:
            notify_emby_refresh()

    if background:
        threading.Thread(target=_run, daemon=True, name='emby-notify-deleted').start()
    else:
        _run()

def notify_emby_refresh():
    for ep in ('/Library/Refresh', '/emby/Library/Refresh'):
        try:
            emby_request(ep, method='POST', timeout=10)
            return True
        except Exception as e:
            log.warning('Emby 刷新失败 %s: %s', ep, e)
    return False

def parse_dt(s):
    if not s: return None
    try:
        return datetime.datetime.fromisoformat(s.split('.')[0].rstrip('Z')).replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None

def _fetch_all_episodes(force=False):
    """分页拉取全库 Episode（带 600 秒缓存 + 线程锁，避免并发重复拉）"""
    if not force and state._ep_cache['data'] is not None and (time.time() - state._ep_cache['ts']) < 600:
        log.info('复用分集缓存（%d 条，%.0f 秒前）', len(state._ep_cache['data']), time.time() - state._ep_cache['ts'])
        return state._ep_cache['data']
    with state._ep_lock:
        if not force and state._ep_cache['data'] is not None and (time.time() - state._ep_cache['ts']) < 600:
            return state._ep_cache['data']
        all_eps = []
        start = 0
        page_size = 5000
        while True:
            data = emby_request('/Items', {
                'Recursive': 'true',
                'IncludeItemTypes': 'Episode',
                'Fields': 'SeriesId,ParentIndexNumber,IndexNumber,Path',
                'StartIndex': start,
                'Limit': page_size,
            }) or {}
            items = data.get('Items') or []
            total = data.get('TotalRecordCount', 0)
            all_eps.extend(items)
            log.info('拉取分集 %d/%d', len(all_eps), total)
            if len(items) < page_size or len(all_eps) >= total:
                break
            start += page_size
        state._ep_cache['ts'] = time.time()
        state._ep_cache['data'] = all_eps
        return all_eps

def _dir_has_media(d):
    """目录里是否还有媒体文件（一次 scandir，遇到就返回）。无法确认（权限/IO 错误）时保守返回 True。"""
    try:
        with os.scandir(d) as it:
            for e in it:
                if os.path.splitext(e.name)[1].lower() in _MEDIA_EXT:
                    return True
        return False
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True

def _alive_dir_map(paths):
    """对一批 Emby 文件路径，按父目录去重后检查是否还有媒体文件：{父目录: bool}。
    只按「目录」判断，与集号无关（只有 S08/S09、没有 S01 的剧也没问题）；
    十万分集通常只有几千个季目录。库根不可用（NAS 掉挂载）或死目录占比异常时整体不信任，
    返回空 dict（= 全部视为存在），避免把整库误判成幽灵。"""
    dirs = {}
    for p in paths:
        if not p:
            continue
        p = p.replace('\\', '/')
        d = p.rsplit('/', 1)[0] if '/' in p else ''
        if not d or d in dirs:
            continue
        conv = state.EMBY_PATHS.to_container(d)
        if conv is None:
            dirs[d] = True
            continue
        root = state.L_ROOT if str(conv).startswith(str(state.L_ROOT) + os.sep) else state.S_ROOT
        dirs[d] = True if not root.exists() else _dir_has_media(conv)
    dead = sum(1 for v in dirs.values() if not v)
    if len(dirs) > 20 and dead / len(dirs) > 0.5:
        log.warning('片库映射：%d/%d 个目录检测为空，疑似挂载异常，本次不做幽灵过滤', dead, len(dirs))
        return {}
    return dirs

def _paged_items(params, page_size=1000, max_items=50000):
    """分页拉取 /Items；任何一页失败都向上抛，由调用方决定是否保留旧缓存。"""
    items, start = [], 0
    while len(items) < max_items:
        data = emby_request('/Items', dict(params, StartIndex=start, Limit=page_size)) or {}
        page = data.get('Items') or []
        items.extend(page)
        total = data.get('TotalRecordCount') or 0
        if len(page) < page_size or (total and len(items) >= total):
            break
        start += page_size
    return items

def _recent(item_type, fields, limit, cutoff):
    """给旧 action_played 用，保留兼容"""
    params = {
        'Recursive': 'true', 'IncludeItemTypes': item_type,
        'Fields': fields, 'SortBy': 'DateCreated', 'SortOrder': 'Descending', 'Limit': limit,
    }
    data = emby_request('/Items', params) or {}
    for it in data.get('Items', []):
        dt = parse_dt(it.get('DateCreated'))
        if dt is None: continue
        if dt < cutoff: break
        yield it

def _src(path):
    lib = state.emby_lib_of(path)
    return '本地影视库' if lib == 'local' else ('分享影视库' if lib == 'share' else '其它库')

def emby_path_to_container(emby_path):
    """将 Emby 返回的 Path 转成容器内路径"""
    # 严格按配置的根目录前缀映射（不做末级目录名兜底），宁可对不上也不能删错库
    return state.EMBY_PATHS.to_container(emby_path)
