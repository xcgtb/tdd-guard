# -*- coding: utf-8 -*-
"""剧集/电影数据统一入口：Emby 库清单、TMDB 身份、实时分集、已播集对照。

此前「订阅追更 / 片库映射 / 影视探索」各写了一套「Series → 分集」实现，缓存 TTL、
Limit、幽灵过滤与库归属口径互不相同，导致同一部剧在三个页面集数对不上。这里收敛
成唯一的数据访问路径，各业务模块只留薄包装返回原有结构：

- ``library_items()``：Series/Movie 全量清单（60 秒内存 TTL，``invalidate()`` 失效）；
- ``tmdb_id_of()``：ProviderIds.Tmdb 优先，其次目录名 ``{tmdb-xxx}`` 磁盘兜底；
- ``series_ids_by_tmdb()``：精确查询 + ProviderIds 校验，再回退全量清单 / fallback_id；
- ``live_episodes()`` / ``series_live()``：某剧当前分集（15 秒微缓存），统一库归属与幽灵过滤；
- ``tmdb_aired_map()`` / ``tmdb_totals()``：已播 / 标称集数的唯一计算口径。

只依赖 state / emby / lib 三个叶子模块，不反向依赖订阅、晨报等业务模块，避免导入环。
"""
import time, threading, logging

from . import state, emby, lib

log = logging.getLogger('media_agent')

LIVE_TTL = 15          # 实时分集微缓存（探索页连点刷新 / 多入口同时检查复用）
EP_LIMIT = 50000       # 单次 /Items 上限；旧的 5000 会把大库分集截断
LIB_TTL = 60           # 库清单内存缓存

_lib_cache = {}        # kind -> {'ts': float, 'data': list}
_lib_lock = threading.Lock()


def _pid(item):
    """取 ProviderIds 里的 TMDB id（键大小写都认：'Tmdb' / 'tmdb'）；没有返回 ''。"""
    pids = item.get('ProviderIds') or {}
    for k, v in pids.items():
        if str(k).lower() == 'tmdb' and v not in (None, ''):
            return str(v)
    return ''


def tmdb_id_of(item, disk_lookup=None):
    """Emby 条目 → TMDB id：ProviderIds 优先，其次目录名 {tmdb-xxx} 兜底。

    disk_lookup 是 lib._disk_tmdb_lookup() 的产物（容器内标题目录路径 → tmdb），
    批量调用时由调用方算一次传入，避免逐条重扫磁盘。
    """
    pid = _pid(item)
    if pid:
        return pid
    path = item.get('Path') or ''
    if not path:
        return ''
    conv = emby.emby_path_to_container(path)
    if conv is None:
        return ''
    if disk_lookup is None:
        disk_lookup = lib._disk_tmdb_lookup()
    return str(disk_lookup.get(str(conv), '') or '')


def library_items(kind):
    """Series / Movie 全量清单（Recursive；字段是探索索引与片库映射的并集）。

    60 秒内存 TTL：一次构建被两个索引复用，避免刚建完探索索引又马上拉一遍全库。
    拉取失败返回空列表且不写缓存，避免把一次网络抖动固化成 60 秒空库。
    """
    with _lib_lock:
        hit = _lib_cache.get(kind)
        if hit and time.time() - hit['ts'] < LIB_TTL:
            return hit['data']
    try:
        data = emby.emby_request('/Items', {
            'Recursive': 'true', 'IncludeItemTypes': kind,
            'Fields': 'ProviderIds,Path,ProductionYear,CommunityRating,ImageTags,Genres',
            'Limit': EP_LIMIT,
        }) or {}
        items = data.get('Items') or []
    except Exception as e:
        log.warning('Emby %s 清单拉取失败: %s', kind, e)
        return []
    with _lib_lock:
        _lib_cache[kind] = {'ts': time.time(), 'data': items}
    return items


def invalidate(tmdb_id=None):
    """失效缓存：库清单总是清；tmdb_id 为空时清全部实时分集，否则只清指定那一部。"""
    with _lib_lock:
        _lib_cache.clear()
    with state._live_eps_lock:
        if tmdb_id is None:
            state._live_eps_cache.clear()
        else:
            state._live_eps_cache.pop(str(tmdb_id), None)


def is_ghost(path):
    """幽灵分集：路径落在本地/分享库根内，但容器内文件与所在目录都已不存在。

    CD2 删源后 Emby 异步刷新期间会残留这类条目，直接信 Emby 会让探索 / 片库映射
    把已删集算成「仍在库里」。判定规则（与 emby._alive_dir_map 同源）：
      - 路径映射不到两个库根 → 不是幽灵（其它库无法核实，保留）；
      - 库根本身不存在（NAS 掉挂载）→ 不做幽灵过滤，避免把整库误判成删除；
      - 文件与父目录都 stat 不到才算幽灵；只要目录还在就保留（分集可能被重新刮削）。
    """
    lib_tag = state.emby_lib_of(path)
    if not lib_tag:
        return False
    conv = state.EMBY_PATHS.to_container(path)
    if conv is None:
        return False
    root = state.L_ROOT if lib_tag == 'local' else state.S_ROOT
    try:
        if not root.exists():
            return False
        if conv.exists() or conv.parent.exists():
            return False
    except OSError:
        return False
    return True


def live_episode_items(series_id):
    """某 Emby Series 条目当前的分集原始条目（已剔除幽灵），供晨报按需读取字段。"""
    if not series_id:
        return []
    try:
        data = emby.emby_request('/Items', {
            'ParentId': series_id, 'Recursive': 'true', 'IncludeItemTypes': 'Episode',
            'Fields': 'ParentIndexNumber,IndexNumber,IndexNumberEnd,DateCreated,Path',
            'Limit': EP_LIMIT,
        }) or {}
        items = data.get('Items') or []
    except Exception as e:
        log.warning('实时分集拉取失败 series=%s: %s', series_id, e)
        return []
    return [ep for ep in items if not is_ghost(ep.get('Path') or '')]


def live_episodes(series_ids):
    """聚合多个 Emby Series 条目的分集集合。

    返回 ``{'episodes','local','share','created','series_ids','empty'}``：
      - episodes：所有非幽灵分集 ``(季,集)`` union（含映射不到库根的，订阅追更用）；
      - local/share：能按路径归属到两个库根的子集（探索 / 片库映射计数用）；
      - created：``(季,集) -> DateCreated``（同一集取较大值，与旧逻辑一致）；
      - empty：没有任何分集时为 True（调用方应保留旧值而不是打成 0）。

    IndexNumberEnd 表示一集连播多集，展开成区间；季 / 集号 <= 0 的跳过。
    """
    ids = [x for x in (series_ids or []) if x]
    have, local, share, created = set(), set(), set(), {}
    for sid in ids:
        for ep in live_episode_items(sid):
            try:
                sn = int(ep.get('ParentIndexNumber') or 0)
                en = int(ep.get('IndexNumber') or 0)
                en_end = int(ep.get('IndexNumberEnd') or en)
            except (TypeError, ValueError):
                continue
            if sn <= 0 or en <= 0:
                continue
            lib_tag = state.emby_lib_of(ep.get('Path') or '')
            for e in range(en, max(en, en_end) + 1):
                have.add((sn, e))
                if lib_tag == 'local':
                    local.add((sn, e))
                elif lib_tag == 'share':
                    share.add((sn, e))
            created[(sn, en)] = max(created.get((sn, en), ''), ep.get('DateCreated') or '')
    return {'episodes': have, 'local': local, 'share': share, 'created': created,
            'series_ids': ids, 'empty': not have}


def _series_items_by_tmdb(tmdb_id, fallback_id=None):
    """按 TMDB id 找匹配的 Series 条目。

    ① AnyProviderIdEquals 精确查询——但服务端可能忽略该过滤参数返回全部 Series，
       所以必须逐条校验 ProviderIds.Tmdb（大小写都认）；② 全量清单按 tmdb_id_of 过滤；
    ③ 仍为空时用 fallback_id 兜底（调用方从探索索引拿到的 Emby 条目 id）。
    """
    want = str(tmdb_id or '')
    if not want:
        return []
    items = []
    try:
        data = emby.emby_request('/Items', {
            'Recursive': 'true', 'IncludeItemTypes': 'Series',
            'Fields': 'ProviderIds,Name,Path', 'Limit': EP_LIMIT,
            'AnyProviderIdEquals': f'Tmdb.{want}',
        }) or {}
        items = [it for it in (data.get('Items') or [])
                 if it.get('Id') and _pid(it) == want]
    except Exception as e:
        log.warning('实时集数：Series 精确查询失败 %s: %s', want, e)
    if not items:
        disk_lookup = lib._disk_tmdb_lookup()
        items = [it for it in library_items('Series')
                 if it.get('Id') and tmdb_id_of(it, disk_lookup) == want]
    if not items and fallback_id:
        items = [{'Id': fallback_id}]
    return items


def series_ids_by_tmdb(tmdb_id, fallback_id=None):
    """按 TMDB id 返回 Emby Series 条目 id 列表（精确查询 + 校验，再兜底）。"""
    return [it['Id'] for it in _series_items_by_tmdb(tmdb_id, fallback_id) if it.get('Id')]


def series_live(tmdb_id, fallback_id=None, use_cache=True):
    """某剧实时分集；15 秒微缓存写 state._live_eps_cache。完全查不到返回 None。

    结果额外带 series_id / series_name，供订阅追更直接使用。
    """
    if not tmdb_id:
        return None
    ck = str(tmdb_id)
    if use_cache:
        with state._live_eps_lock:
            hit = state._live_eps_cache.get(ck)
            if hit and time.time() - hit[0] < LIVE_TTL:
                return hit[1]
    items = _series_items_by_tmdb(tmdb_id, fallback_id)
    ids = [it['Id'] for it in items if it.get('Id')]
    if not ids:
        return None
    out = live_episodes(ids)
    out['series_id'] = ids[0]
    out['series_name'] = next((it.get('Name') for it in items if it.get('Name')), '')
    if use_cache:
        with state._live_eps_lock:
            state._live_eps_cache[ck] = (time.time(), out)
            # 轻量清理：只保留最近 2000 条，避免长跑进程内存无界增长
            if len(state._live_eps_cache) > 2000:
                for k, _v in sorted(state._live_eps_cache.items(), key=lambda kv: kv[1][0])[:500]:
                    state._live_eps_cache.pop(k, None)
    return out


def tmdb_aired_map(info):
    """TMDB ``/tv/{id}`` 原始响应 → ``{季: 已播集数}``（唯一已播口径）。

    以 last_episode_to_air 为界：之前的季整季计入，最后播出的季只计到该集，
    之后的季（尚未开播）忽略；没有该字段时退回「各季 episode_count 全部计入」。
    S00（特别篇）与 <=0 的季、集数为 0 的季一律排除。
    """
    counts = {}
    for ss in ((info or {}).get('seasons') or []):
        sn = ss.get('season_number')
        try:
            if sn is None or int(sn) <= 0:
                continue
            ec = int(ss.get('episode_count', 0) or 0)
        except (TypeError, ValueError):
            continue
        if ec > 0:
            counts[int(sn)] = ec
    last = (info or {}).get('last_episode_to_air') or {}
    try:
        last_s = int(last.get('season_number') or 0)
        last_e = int(last.get('episode_number') or 0)
    except (TypeError, ValueError):
        last_s = last_e = 0
    if last_s > 0 and last_e > 0:
        counts = {sn: (last_e if sn == last_s else n) for sn, n in counts.items() if sn <= last_s}
    return counts


def tmdb_totals(info):
    """``{'aired': 已播集数, 'declared': 标称总集数（含未播，展示口径）}``。"""
    declared = 0
    for ss in ((info or {}).get('seasons') or []):
        sn = ss.get('season_number')
        try:
            if sn is None or int(sn) <= 0:
                continue
            ec = int(ss.get('episode_count', 0) or 0)
        except (TypeError, ValueError):
            continue
        if ec > 0:
            declared += ec
    return {'aired': sum(tmdb_aired_map(info).values()), 'declared': declared}
