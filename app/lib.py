# -*- coding: utf-8 -*-
"""磁盘扫描与治理身份（Lib 类 + 双库遍历缓存 + tmdb 磁盘索引），从 engine.py 拆出。

注意：L_ROOT / S_ROOT / _CATEGORY_NAMES 是 engine 里的运行时可变全局，这里一律通过
_engine_ns() 惰性访问，保证 reload_config / monkeypatch 能穿透。
"""
import os
import re
import time
import logging
import threading
from pathlib import Path
from collections import defaultdict

log = logging.getLogger('media_agent')

try:
    from .core import parse_season_dir, get_ep, governance_title_key
except ImportError:
    from core import parse_season_dir, get_ep, governance_title_key


def _engine_ns():
    try:
        from . import engine as _e
        return _e
    except ImportError:
        try:
            import engine as _e
            return _e
        except ImportError:
            return None


class Lib:
    def __init__(self, root):
        self.root = root
        self.mov = defaultdict(list)
        self.tv = defaultdict(lambda: defaultdict(list))
        self.meta = {}
        self.by_base = defaultdict(set)
        # TMDB 的电影与剧集是两个独立编号空间（movie:103 ≠ tv:103），
        # 因此键带类型前缀，避免跨类型误聚合/误配对。
        self.tmdb_refs = defaultdict(set)  # 键: 'movie:<id>' / 'tv:<id>' -> 治理 key 集合
        self.key_tmdb_movie = {}  # 治理 key -> 电影类文件携带的 TMDB ID（tmdb_first 策略用）
        self.key_tmdb_tv = {}     # 治理 key -> 剧集类文件携带的 TMDB ID
        self.path_tmdb = {}       # 标题目录绝对路径(容器内) -> tmdb_id（片库映射/探索兜底用）
        self.strm_count = 0
        if not root.exists(): return
        for dirpath, _dirs, names in os.walk(str(root)):
            strms = sorted(n for n in names if n.endswith('.strm'))
            if not strms:
                continue
            d = Path(dirpath)
            pname = d.name
            folder = d.parent.name if parse_season_dir(pname) is not None else pname
            key, disp, base, year = governance_title_key(folder)
            self.meta[key] = (disp, base, year)
            self.by_base[base].add(key)
            tm = re.search(r'(?i)tmdb(?:id)?[=\-: ]*(\d+)', folder or '')
            if tm:
                # 标题目录 = 季目录的父目录；电影目录就是它自己。
                title_dir = d.parent if parse_season_dir(pname) is not None else d
                self.path_tmdb[str(title_dir)] = tm.group(1)
            for n in strms:
                self.strm_count += 1
                f = d / n
                # 裸 EP/Episode 只在明确的剧集上下文中启用；电影目录中的
                # `Star Wars Ep 4` 不得被误判成 S01E04。季目录天然是强上下文，
                # 扁平剧集则由剧集分类路径提供上下文。
                allow_bare_ep = (parse_season_dir(pname) is not None or _under_tv_category(d, root))
                ep = get_ep(n, pname, allow_bare_ep=allow_bare_ep)
                if ep:
                    self.tv[key][ep[0]].append(f)
                    if tm:
                        self.tmdb_refs['tv:' + tm.group(1)].add(key)
                        self.key_tmdb_tv.setdefault(key, tm.group(1))
                else:
                    self.mov[key].append(f)
                    if tm:
                        self.tmdb_refs['movie:' + tm.group(1)].add(key)
                        self.key_tmdb_movie.setdefault(key, tm.group(1))

    def find(self, meta, container):
        key, (_, base, year) = meta
        if key in container: return key
        # 治理身份是「剧名 + 年份」。年份已知时绝不允许跨年模糊配对；
        # 年份未知时也只允许与同样未知年份的目录配对。
        cands = [k for k in self.by_base.get(base, ())
                 if k in container and self.meta[k][2] == year]
        return cands[0] if len(cands) == 1 else None

def _disk_tmdb_lookup():
    """合并两库磁盘扫描的「标题目录绝对路径 -> tmdb_id」兜底表。

    上游 TgtoDrive 把 tmdb 写在目录名 ``{tmdb-xxx}``，Emby 刮削不写
    ``ProviderIds.Tmdb``，导致片库映射/探索索引拿不到 tmdb_id。这里返回
    磁盘目录名里的事实 tmdb，供两处在 Emby ProviderIds 缺失时兜底。
    """
    lookup = {}
    try:
        for lib in (_get_lib(_engine_ns().L_ROOT), _get_lib(_engine_ns().S_ROOT)):
            lookup.update(lib.path_tmdb)
    except Exception as e:
        log.warning('磁盘 tmdb 兜底表构建失败: %s', e)
    return lookup

def _disk_eps_by_tmdb():
    """按目录名 tmdb 聚合两库磁盘分集：tmdb_id -> {episodes/local_eps/share_eps}。

    上游 TgtoDrive 用 SxxExx 命名分集，磁盘文件名是权威；Emby 识别可能漏集/错集，
    导致片库映射的缺集对照误判。这里复用 Lib 磁盘索引，把磁盘事实分集按 tmdb_id
    聚合，供片库映射在 Emby 分集之上 union 兜底。
    """
    out = defaultdict(lambda: {'episodes': set(), 'local_eps': set(), 'share_eps': set()})
    try:
        for lib, tag in ((_get_lib(_engine_ns().L_ROOT), 'local'), (_get_lib(_engine_ns().S_ROOT), 'share')):
            for key, tid in lib.key_tmdb_tv.items():
                for sn, files in lib.tv.get(key, {}).items():
                    if sn <= 0:
                        continue
                    for f in files:
                        try:
                            ep = get_ep(f.name, f.parent.name, allow_bare_ep=True)
                        except Exception:
                            continue
                        if ep and ep[0] > 0 and ep[1] > 0:
                            out[tid]['episodes'].add(ep)
                            (out[tid]['local_eps'] if tag == 'local'
                             else out[tid]['share_eps']).add(ep)
    except Exception as e:
        log.warning('磁盘分集表构建失败: %s', e)
    return out

def _match_governance_key(S, L, skey, container, strategy, kind):
    """跨库配对（分享 key -> 本地 key）。

    ``title_year``（默认）：按「剧名 + 年份」配对，TMDB 完全不参与（安全档）。
    ``tmdb_first``：优先按 TMDB ID 配对——两边 TMDB 一致且本地唯一时直接配对
    （可跨越译名/命名差异）；同一 TMDB 在本地对应多个不同身份时判定为冲突、
    不自动配，交给 ``_identity_conflicts`` 人工处理；无 TMDB 或本地无对应时
    回退到「剧名 + 年份」。

    ``kind``：'movie' 或 'tv'。TMDB 的电影与剧集是两个独立编号空间
    （movie:103 ≠ tv:103），必须同类型匹配，否则会把不同作品误判成冲突。
    """
    if strategy == 'tmdb_first':
        s_map = S.key_tmdb_movie if kind == 'movie' else S.key_tmdb_tv
        stm = s_map.get(skey)
        if stm:
            cands = [k for k in L.tmdb_refs.get(kind + ':' + stm, ()) if k in container]
            if len(cands) == 1:
                return cands[0]
            if len(cands) > 1:
                # 同 TMDB 多个身份：存疑，宁可不配也不配错
                return None
            # 0 个候选 -> 回退剧名 + 年份
    return L.find((skey, S.meta[skey]), container)

_lib_cache = {}
_lib_cache_lock = threading.Lock()
LIB_CACHE_TTL = 30


def _get_lib(root: Path) -> Lib:
    key = str(root)
    now = time.time()
    with _lib_cache_lock:
        hit = _lib_cache.get(key)
        if hit and now - hit[0] < LIB_CACHE_TTL:
            return hit[1]
    lib = Lib(root)
    with _lib_cache_lock:
        _lib_cache[key] = (now, lib)
    return lib

def _invalidate_lib_cache():
    with _lib_cache_lock:
        _lib_cache.clear()

def _normalize_title(name: str) -> str:
    """规范化目录/文件名：去 tmdb 后缀、全角转半角、所有分隔符统一成单空格"""
    if not name: return ''
    name = re.sub(r'\{?tmdb[-_:=\s]*\d+\}?', '', name, flags=re.I)
    name = name.replace('：', ':').replace('（', '(').replace('）', ')')
    name = re.sub(r'[_\-\.:\s]+', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name

def _find_dir_fuzzy(parent, target_name):
    """
    在 parent 下找最接近 target_name 的子目录。
    返回 (dir_or_None, level)，level ∈ {exact, normalized, fuzzy, ambiguous, none}：
      - exact       目录名完全相同        -> 高置信度
      - normalized  标准化后完全相等      -> 高置信度
      - fuzzy       前缀匹配且唯一候选    -> 低置信度（保留发现能力）
      - ambiguous   前缀匹配多候选        -> 禁止自动删
      - none        找不到                -> 禁止
    """
    if not parent.is_dir():
        return None, 'none'
    target_norm = _normalize_title(target_name)
    if not target_norm:
        return None, 'none'

    exact = parent / target_name
    if exact.is_dir():
        return exact, 'exact'

    for child in parent.iterdir():
        if child.is_dir() and _normalize_title(child.name) == target_norm:
            return child, 'normalized'

    prefix_matches = []
    try:
        for child in parent.iterdir():
            if not child.is_dir():
                continue
            cn = _normalize_title(child.name)
            if cn and (cn.startswith(target_norm) or target_norm.startswith(cn)):
                prefix_matches.append(child)
    except OSError:
        return None, 'none'
    if len(prefix_matches) == 1:
        return prefix_matches[0], 'fuzzy'
    if len(prefix_matches) > 1:
        return None, 'ambiguous'
    return None, 'none'

def _under_tv_category(d, root):
    try:
        parts = d.relative_to(root).parts
    except ValueError:
        return False
    return any(p in _engine_ns()._CATEGORY_NAMES and '剧' in p for p in parts)
