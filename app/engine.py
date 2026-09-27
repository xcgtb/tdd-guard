# -*- coding: utf-8 -*-
"""media_agent.py —— 飞牛 NAS 端治理引擎"""
import os, re, sys, json, threading, shlex, shutil, fcntl, time, argparse, datetime, hashlib, logging, traceback
import urllib.parse, urllib.request, urllib.error
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass, field

try:
    from .core import (
        esc, parse_season_dir, get_ep, get_score, best_score,
        title_key, is_exempt, fmt_nums, analyze_season_episodes, is_seq,
        parse_emby_library,
    )
    from . import config as _cfg
except ImportError:
    from core import (
        esc, parse_season_dir, get_ep, get_score, best_score,
        title_key, is_exempt, fmt_nums, analyze_season_episodes, is_seq,
        parse_emby_library,
    )
    import config as _cfg


try:
    from . import logger
except ImportError:
    import logger


def _p(env, default):
    return Path(os.environ.get(env, default))

L_ROOT = _p('L_ROOT', '/media/local')
S_ROOT = _p('S_ROOT', '/media/share')
CLOUD_L_ROOT = _p('CLOUD_L_ROOT', '/media/cloud')
# DATA_DIR 以 config 为准（config.py 是路径的唯一定义处），避免两处默认值漂移
DATA_DIR = _cfg.DATA_DIR
LOG_TXT = DATA_DIR / '媒体治理明细.log'
STATE_DIR = DATA_DIR / 'state'
TRASH_DIR = DATA_DIR / 'trash'
LOCK_FILE = DATA_DIR / 'agent.lock'
REPORT_DIR = DATA_DIR / 'reports'
SUB_STATE_FILE = STATE_DIR / 'subscriptions_state.json'
EMBY_LIB_CACHE_FILE = STATE_DIR / 'emby_library_with_tmdb.json'
INGEST_CACHE_FILE = STATE_DIR / 'ingest_cache.json'

VIDEO_EXTS = ['.mkv', '.mp4', '.ts', '.mov', '.iso', '.m2ts']

# ═══════════════ 目录生命周期 / 孤儿治理（白皮书 §15+§16）═══════════════
_CATEGORY_NAMES = {
    '电影', '剧集', '儿童节目', '综艺', '动漫', '纪录片', '演唱会',
    '国产电影', '华语电影', '欧美电影', '日韩电影', '动画电影', '外语电影', '其他电影',
    '国产剧集', '欧美剧集', '日韩剧集', '其他剧集',
    '国产剧', '欧美剧', '日韩剧',
}
_METADATA_EXTS = {
    '.nfo', '.jpg', '.jpeg', '.png', '.gif', '.webp',
    '.srt', '.ass', '.ssa', '.sub', '.idx', '.sup',
    '.json', '.url', '.xml', '.md5', '.sha1',
}
TRASH_STRM = os.environ.get('TRASH_STRM', '1') == '1'
TRASH_KEEP_DAYS = int(os.environ.get('TRASH_KEEP_DAYS', '14'))
PLAN_TTL = 2 * 3600
PRUNE_MIN_DEPTH = int(os.environ.get('PRUNE_MIN_DEPTH', '3'))

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('media_agent')


RUNTIME_CFG = _cfg.load_config()
EMBY_HOST = RUNTIME_CFG['emby_host']
EMBY_KEY  = RUNTIME_CFG['emby_key']
TMDB_BASE = os.environ.get('TMDB_BASE', 'https://api.themoviedb.org/3')
TMDB_LANG = os.environ.get('TMDB_LANG', 'zh-CN')
TMDB_IMG  = os.environ.get('TMDB_IMG', 'https://image.tmdb.org/t/p/w500')
TMDB_INFO_TTL = 24 * 3600


def reload_config():
    global RUNTIME_CFG, EMBY_HOST, EMBY_KEY
    RUNTIME_CFG = _cfg.load_config()
    EMBY_HOST = RUNTIME_CFG['emby_host'] or 'http://127.0.0.1:8096'
    EMBY_KEY = RUNTIME_CFG['emby_key'] or ''
    return RUNTIME_CFG


def _strategy():
    return _cfg.get_strategy()

def _exempt_keywords():
    return _strategy()['exempt_keywords']

def _special_action():
    return _strategy().get('special_action', 'compare')

def _ep(p: Path):
    return get_ep(p.name, p.parent.name)

def _best(files):
    return best_score(f.name for f in files)

def _exempt(name):
    kws = _exempt_keywords()
    return any(k in str(name) for k in kws)

def _exempt_hit(name):
    kws = _exempt_keywords()
    return [k for k in kws if k in str(name)]

def _analyze(eps, name=''):
    return analyze_season_episodes(eps, name, _exempt_keywords())

def _is_seq(eps, name=''):
    return is_seq(eps, name, _exempt_keywords())

def _share_wins(s_q, l_q):
    s = _strategy()
    decision = s['decision']
    if decision == 'keep_local': return False
    if decision == 'keep_share': return True
    return s_q > l_q if s['tie_keep_local'] else s_q >= l_q


# ═══════════════════ 电视剧逐集比较（白皮书 §7）═══════════════════
def _season_replaceable(s_files, l_files, disp=''):
    """
    判断分享 Season 能否整体替换本地 Season（白皮书 §7.1）：
      1. 分享 Season 必须存在
      2. 本地每个正片 Episode 都必须在分享里找到
      3. 每一集分享质量必须 >= 本地质量
    同集号多份时取最高质量那份参与比较。
    返回 (replaceable: bool, reason: str)
    """
    l_eps, s_eps = {}, {}
    for f in (l_files or []):
        ep = _ep(f)
        if not ep or ep[0] <= 0:
            continue
        old = l_eps.get(ep[1])
        if old is None or get_score(f.name) > get_score(old.name):
            l_eps[ep[1]] = f
    for f in (s_files or []):
        ep = _ep(f)
        if not ep or ep[0] <= 0:
            continue
        old = s_eps.get(ep[1])
        if old is None or get_score(f.name) > get_score(old.name):
            s_eps[ep[1]] = f

    if not l_eps:
        return False, '本地无正片集'
    if not s_eps:
        return False, '分享无正片集'

    missing = sorted(set(l_eps) - set(s_eps))
    if missing:
        return False, '分享缺集 ' + fmt_nums(missing)

    weak = sorted(
        e for e in l_eps
        if get_score(s_eps[e].name) < get_score(l_eps[e].name)
    )
    if weak:
        return False, '分享画质不足 ' + fmt_nums(weak)

    return True, '逐集覆盖且质量达标'


# ═══════════════════ Emby ═══════════════════
def emby_request(path, params=None, method='GET', timeout=15):
    if not EMBY_KEY:
        raise RuntimeError('未配置 EMBY_KEY')
    url = EMBY_HOST + path + ('?' + urllib.parse.urlencode(params) if params else '')
    req = urllib.request.Request(url, method=method, data=b'' if method == 'POST' else None,
                                 headers={'X-Emby-Token': EMBY_KEY})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw.decode('utf-8')) if (method == 'GET' and raw) else None


def notify_emby_refresh():
    for ep in ('/Library/Refresh', '/emby/Library/Refresh'):
        try:
            emby_request(ep, method='POST', timeout=10)
            return True
        except Exception as e:
            log.warning('Emby 刷新失败 %s: %s', ep, e)
    return False


def notify_telegram(text, chat_id=None):
    token = (RUNTIME_CFG.get('telegram_bot_token') or '').strip()
    cid = chat_id or (RUNTIME_CFG.get('telegram_chat_id') or '').strip()
    if not token or not cid:
        return False
    try:
        url = f'https://api.telegram.org/bot{token}/sendMessage'
        data = urllib.parse.urlencode({'chat_id': cid, 'text': text, 'parse_mode': 'HTML',
                                       'disable_web_page_preview': 'true'}).encode()
        req = urllib.request.Request(url, data=data, method='POST')
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except Exception as e:
        log.warning('Telegram 推送失败: %s', e)
        return False


def parse_dt(s):
    if not s: return None
    try:
        return datetime.datetime.fromisoformat(s.split('.')[0].rstrip('Z')).replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def write_audit_log(category, title, details=None):
    try:
        logger.write(category, title, details)
    except Exception as e:
        log.warning('审计日志写入失败: %s', e)

class Lib:
    def __init__(self, root):
        self.root = root
        self.mov = defaultdict(list)
        self.tv = defaultdict(lambda: defaultdict(list))
        self.meta = {}
        self.by_base = defaultdict(set)
        self.strm_count = 0
        if not root.exists(): return
        for f in root.rglob('*.strm'):
            self.strm_count += 1
            folder = f.parent.parent.name if parse_season_dir(f.parent.name) is not None else f.parent.name
            key, disp, base, year = title_key(folder)
            self.meta[key] = (disp, base, year)
            self.by_base[base].add(key)
            ep = _ep(f)
            if ep: self.tv[key][ep[0]].append(f)
            else: self.mov[key].append(f)

    def find(self, meta, container):
        key, (_, base, year) = meta
        if key in container: return key
        cands = [k for k in self.by_base.get(base, ()) if k in container and (year is None or self.meta[k][2] in (None, year))]
        return cands[0] if len(cands) == 1 else None


# ═══════════════════ Lib 短时效缓存 ═══════════════════
# 场景：用户点「扫描双库」→ 生成计划 → 立刻点「执行清理」
# 两次操作都调 build_plan()，会重复对双库做全量 rglob。
# 加 30 秒缓存后，第二次直接复用，大库上省几秒到几十秒。
# 清理成功后主动失效，保证不会用到陈旧数据。
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


_ep_cache = {'ts': 0, 'data': None}
_ep_lock = threading.Lock()

def _fetch_all_episodes(force=False):
    """分页拉取全库 Episode（带 600 秒缓存 + 线程锁，避免并发重复拉）"""
    import threading as _th
    if not force and _ep_cache['data'] is not None and (time.time() - _ep_cache['ts']) < 600:
        log.info('复用分集缓存（%d 条，%.0f 秒前）', len(_ep_cache['data']), time.time() - _ep_cache['ts'])
        return _ep_cache['data']
    with _ep_lock:
        if not force and _ep_cache['data'] is not None and (time.time() - _ep_cache['ts']) < 600:
            return _ep_cache['data']
        all_eps = []
        start = 0
        page_size = 5000
        while True:
            data = emby_request('/Items', {
                'Recursive': 'true',
                'IncludeItemTypes': 'Episode',
                'Fields': 'SeriesId,ParentIndexNumber,IndexNumber',
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
        _ep_cache['ts'] = time.time()
        _ep_cache['data'] = all_eps
        return all_eps


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

def cloud_videos(f, base_root, cloud_root):
    """
    查找 strm 对应的云端视频文件（白皮书 §13 分级）。
    返回 (files, confidence)：
      - 'exact'           精确文件名/扩展名匹配 -> 允许自动删
      - 'normalized'      标准化后唯一匹配     -> 允许自动删
      - 'ambiguous'       标准化后多候选       -> 禁止自动删
      - 'unique_fallback' 目录里只有 1 个视频  -> 禁止自动删
      - 'none'            找不到              -> 禁止自动删
    """
    try:
        rel_parent = f.relative_to(base_root).parent
    except ValueError:
        return [], 'none'

    # ── 逐级解析目录，累积路径置信度 ──
    # 任何一环是 fuzzy -> 整个路径降级；任一环 ambiguous -> 直接返回
    d = cloud_root
    path_level = 'exact'
    for part in rel_parent.parts:
        if not d.is_dir():
            return [], 'none'
        next_d = d / part
        if next_d.is_dir():
            d = next_d
            continue
        sub, level = _find_dir_fuzzy(d, part)
        if sub is None:
            return [], level  # ambiguous / none
        d = sub
        if level == 'fuzzy':
            path_level = 'fuzzy'
        elif level == 'normalized' and path_level != 'fuzzy':
            path_level = 'normalized'

    if not d.is_dir():
        return [], 'none'

    # ── 精确文件名匹配 ──
    # STRM 命名约定：<原名>.<编码信息>.strm，视频扩展名被剥离
    stem = f.stem
    exact = []
    for ext in VIDEO_EXTS:
        c = d / f'{stem}{ext}'
        if c.is_file() and _inside(c, cloud_root):
            exact.append(c)
    if exact:
        if path_level == 'fuzzy':
            # 路径模糊 -> 即使文件名精确也降级为低置信度
            return exact, 'unique_fallback'
        return exact, 'exact'

    # ── 标准化后唯一匹配 ──
    stem_norm = _normalize_title(stem)
    norm_matches = []
    try:
        for child in d.iterdir():
            if not child.is_file(): continue
            if child.suffix.lower() not in VIDEO_EXTS: continue
            if not _inside(child, cloud_root): continue
            if _normalize_title(child.stem) == stem_norm:
                norm_matches.append(child)
    except OSError:
        return [], 'none'
    if len(norm_matches) == 1:
        if path_level == 'fuzzy':
            return norm_matches, 'unique_fallback'
        return norm_matches, 'normalized'
    if len(norm_matches) > 1:
        return norm_matches, 'ambiguous'

    # ── 目录唯一视频兜底（低置信度，仅报告） ──
    try:
        vids = [c for c in d.iterdir()
                if c.is_file() and c.suffix.lower() in VIDEO_EXTS
                and _inside(c, cloud_root)]
        if len(vids) == 1:
            return vids, 'unique_fallback'
    except OSError:
        pass
    return [], 'none'


def _inside(p, root):
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _remove_strm(f, base_root):
    if TRASH_STRM:
        dest = TRASH_DIR / datetime.date.today().isoformat() / base_root.name / f.relative_to(base_root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), str(dest))
    else:
        f.unlink()


def _classify_dir(path, base_root):
    """
    判断目录类型（白皮书 §15）：
      lib_root / category -> 永不自动删
      season / series_root / movie -> 媒体专属目录，可删
      unknown -> 不动
    """
    try:
        rel = path.relative_to(base_root)
    except (ValueError, OSError):
        return 'unknown'
    parts = rel.parts
    if len(parts) == 0:
        return 'lib_root'
    if len(parts) == 1:
        return 'category' if parts[0] in _CATEGORY_NAMES else 'unknown'
    name = path.name
    parent = path.parent
    parent_name = parent.name if parent != base_root else ''
    if parse_season_dir(name) is not None:
        return 'season'
    if parent_name and parse_season_dir(parent_name) is not None:
        return 'series_root'
    if parent_name in _CATEGORY_NAMES:
        return 'movie'
    return 'unknown'


def _dir_cleanable(d):
    """目录（递归）内是否只含 metadata 文件/空目录。返回 (ok, reason)"""
    try:
        for f in d.rglob('*'):
            if not f.is_file():
                continue
            ext = f.suffix.lower()
            if ext == '.strm' or ext in VIDEO_EXTS:
                return False, '还有媒体文件 ' + f.name
            if ext not in _METADATA_EXTS:
                return False, '含未知文件 ' + f.name
    except OSError as e:
        return False, '权限错误 %s' % e
    return True, ''


def _prune_up(d, base_root, cloud_root):
    """
    媒体生命周期目录清理（白皮书 §15）：
      - 只清 movie / season / series_root
      - 遇 category / lib_root 立即停
      - 目录内还有媒体或未知文件 -> 不删
    """
    cur = d
    while cur != base_root:
        try:
            rel = cur.relative_to(base_root)
        except (ValueError, OSError):
            return
        if len(rel.parts) < PRUNE_MIN_DEPTH:
            return
        kind = _classify_dir(cur, base_root)
        if kind in ('category', 'lib_root', 'unknown'):
            return
        if not cur.exists():
            cur = cur.parent
            continue
        ok, reason = _dir_cleanable(cur)
        if not ok:
            log.info('目录跳过清理 [%s]: %s (原因: %s)', kind, cur, reason)
            return
        try:
            shutil.rmtree(cur, ignore_errors=True)
            log.info('已清理空目录 [%s]: %s', kind, cur)
        except OSError as e:
            log.warning('目录删除失败 %s: %s', cur, e)
            return
        if cloud_root and kind in ('movie', 'series_root', 'season'):
            try:
                (cloud_root / rel).rmdir()
            except OSError:
                pass
        cur = cur.parent



def _unlink_with_timeout(v, timeout=15):
    """删文件，最多等 timeout 秒。超时 kill 子进程并返回 False。
    CD2 删 115 大文件很慢（几分钟），不能让它阻塞主任务"""
    import subprocess
    try:
        r = subprocess.run(['rm', '-f', str(v)], timeout=timeout, capture_output=True)
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        log.warning('删除超时(%d秒)，跳过: %s', timeout, v)
        return False
    except Exception as e:
        log.warning('删除失败 %s: %s', v, e)
        return False


def safe_delete_files(files, base_root, cloud_root=None, dry_run=False):
    """
    白皮书 §13 fail-safe 删除链：
      - 只有 exact / normalized 允许自动删云端源
      - ambiguous / unique_fallback / none -> 不动云端、不动 STRM，仅记账
      - 云端删除失败 -> STRM 也不删
    分享库调用时 cloud_root=None，直接删 STRM。
    """
    st = {'strm_removed': 0, 'cloud_removed': 0,
          'cloud_missing': 0, 'cloud_ambiguous': 0, 'cloud_fallback': 0,
          'errors': []}
    parents = set()
    for f in files:
        if f.suffix.lower() != '.strm' or not _inside(f, base_root):
            st['errors'].append(f'跳过非法路径: {f}')
            continue

        if cloud_root is not None:
            if not cloud_root.exists():
                st['cloud_missing'] += 1
                st['errors'].append(f'{f.name}: 云端根目录不可用 ({cloud_root})，STRM 保留')
                continue
            vids, conf = cloud_videos(f, base_root, cloud_root)
            if conf == 'none':
                st['cloud_missing'] += 1
                st['errors'].append(f'{f.name}: 未找到云端源文件，STRM 保留')
                continue
            if conf == 'unique_fallback':
                st['cloud_fallback'] += 1
                st['errors'].append(f'{f.name}: 仅目录唯一兜底匹配（低置信度），STRM 保留')
                continue
            if conf == 'ambiguous':
                st['cloud_ambiguous'] += 1
                st['errors'].append(f'{f.name}: 云端多个候选文件，STRM 保留')
                continue
            # exact / normalized -> 允许自动删
        else:
            vids, conf = [], None

        if dry_run:
            st['strm_removed'] += 1
            st['cloud_removed'] += len(vids)
            continue

        ok = True
        for v in vids:
            if _unlink_with_timeout(v):
                st['cloud_removed'] += 1
            else:
                ok = False
                st['errors'].append(f'{v.name}: 删除超时或失败')
        if not ok:
            continue  # 云端没删成功 -> STRM 保留

        try:
            _remove_strm(f, base_root); st['strm_removed'] += 1
            parents.add(f.parent)
        except OSError as e:
            st['errors'].append(f'{f.name}: {e}')
    for p in sorted(parents, key=lambda x: len(x.parts), reverse=True):
        _prune_up(p, base_root, cloud_root)
    return st


def scan_orphans(max_depth=3):
    """
    扫描两库，返回未知文件（只报告不删，白皮书 §16）。
    用 os.walk + 深度剪枝，避免遍历大库下所有 STRM。
    """
    orphans = []
    try:
        max_depth = int(max_depth)
    except (ValueError, TypeError):
        max_depth = 3
    if max_depth < 1: max_depth = 1
    if max_depth > 10: max_depth = 10

    for root, lib_name in ((L_ROOT, 'local'), (S_ROOT, 'share')):
        if not root.exists():
            continue
        root_parts = len(root.parts)
        try:
            for dp, dns, fns in os.walk(root, topdown=True):
                cur_depth = len(Path(dp).parts) - root_parts
                if cur_depth > max_depth:
                    dns[:] = []
                    continue
                for fn in fns:
                    ext = os.path.splitext(fn)[1].lower()
                    if (not ext) or ext == '.strm' \
                       or ext in VIDEO_EXTS or ext in _METADATA_EXTS:
                        continue
                    fp = os.path.join(dp, fn)
                    try:
                        sz = os.path.getsize(fp)
                    except OSError:
                        sz = 0
                    orphans.append({
                        'lib': lib_name,
                        'path': fp,
                        'ext': ext,
                        'size': sz,
                    })
        except OSError as e:
            log.warning('孤儿扫描失败 %s: %s', root, e)
    return orphans


def action_scan_orphans(args):
    depth = getattr(args, 'max_depth', 3)
    try:
        depth = int(depth)
    except (ValueError, TypeError):
        depth = 3
    if depth < 1: depth = 1
    if depth > 10: depth = 10
    items = scan_orphans(max_depth=depth)
    by_lib = defaultdict(int)
    by_ext = defaultdict(int)
    for it in items:
        by_lib[it['lib']] += 1
        by_ext[it['ext']] += 1
    return {
        'status': 'success',
        'count': len(items),
        'by_lib': dict(by_lib),
        'by_ext': dict(by_ext),
        'items': items[:200],
    }


def purge_old():
    cut = time.time() - TRASH_KEEP_DAYS * 86400
    if TRASH_DIR.exists():
        for d in TRASH_DIR.iterdir():
            try:
                if d.is_dir() and d.stat().st_mtime < cut:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError: pass
    if STATE_DIR.exists():
        plan_cut = time.time() - 7 * 86400
        for f in STATE_DIR.glob('plan_*.json'):
            try:
                if f.stat().st_mtime < plan_cut:
                    f.unlink()
            except OSError:
                pass


@dataclass
class Act:
    kind: str
    text: str
    detail: str
    files: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    @property
    def media_key(self):
        title = self.meta.get('title', '')
        if not title:
            return ''
        season = self.meta.get('season')
        if season is None:
            return f'movie:{title}'
        return f'tv:{title}:S{int(season):02d}'

    @property
    def action_id(self):
        if not self.media_key:
            return ''
        return f'{self.kind}:{self.media_key}'

    @property
    def key(self):
        return self.action_id or f'{self.kind}|{self.text}'


def _act_to_dict(a: Act) -> dict:
    return {
        'text': a.text, 'detail': a.detail,
        'reason': a.meta.get('reason', ''),
        'reason_label': a.meta.get('reason_label', ''),
        'title': a.meta.get('title', ''),
        'season': a.meta.get('season'),
        'meta': a.meta, 'files_count': len(a.files),
    }


def build_plan():
    S, L = _get_lib(S_ROOT), _get_lib(L_ROOT)
    s = _strategy()
    multi_protect = s['multi_season_protect']
    acts = []

    for key, s_files in S.mov.items():
        meta = (key, S.meta[key])
        lk = L.find(meta, L.mov)
        if not lk: continue
        disp, l_files = S.meta[key][0], L.mov[lk]

        hit_kws = _exempt_hit(disp)
        if not hit_kws:
            for files in (s_files, l_files):
                for f in files:
                    hit_kws = _exempt_hit(str(f))
                    if hit_kws: break
                if hit_kws: break
        if hit_kws:
            acts.append(Act('exempt', f'🛡️ 《{disp}》 (白名单豁免)',
                            f'├─ 🛡️ 《{disp}》: 命中白名单 [{",".join(hit_kws)}] ➔ 跳过清理',
                            s_files + l_files,
                            meta={'reason': 'whitelist', 'reason_label': '白名单豁免',
                                  'title': disp, 'keywords': hit_kws}))
            continue

        if _share_wins(_best(s_files), _best(l_files)):
            acts.append(Act('loc', f'🎬 《{disp}》 (分享画质达标，穿透删本地腾网盘)',
                            f'├─ 🎬 《{disp}》: 分享画质达标 ➔ CD2联动删除115网盘旧源', l_files,
                            meta={'reason': 'share_better', 'reason_label': '分享画质达标', 'title': disp}))
        else:
            acts.append(Act('shr', f'🎬 《{disp}》 (分享画质劣于本地，淘汰分享)',
                            f'├─ 🎬 《{disp}》: 分享画质次级 ➔ 清理分享影视库strm', s_files,
                            meta={'reason': 'local_better', 'reason_label': '本地画质更优', 'title': disp}))

    for key, s_seasons in S.tv.items():
        disp = S.meta[key][0]
        lk = L.find((key, S.meta[key]), L.tv)
        l_seasons = L.tv[lk] if lk else {}

        hit_kws = _exempt_hit(disp)
        if not hit_kws:
            for files_map in (s_seasons, l_seasons):
                for files in files_map.values():
                    for f in files:
                        hit_kws = _exempt_hit(str(f))
                        if hit_kws: break
                    if hit_kws: break
                if hit_kws: break
        if hit_kws:
            for sn, s_files in sorted(s_seasons.items()):
                tag = f'《{disp}》S{sn:02d}'
                acts.append(Act('exempt', f'🛡️ {tag} (白名单豁免)',
                                f'├─ 🛡️ {tag}: 命中白名单 [{",".join(hit_kws)}] ➔ 跳过清理',
                                s_files,
                                meta={'reason': 'whitelist', 'reason_label': '白名单豁免',
                                      'title': disp, 'season': sn, 'keywords': hit_kws}))
            continue

        s00_files = s_seasons.get(0)
        l00_files = l_seasons.get(0)
        special_action = s.get('special_action', 'compare')

        if special_action == 'delete':
            # 清理档：本地+分享都删
            tag = f'《{disp}》S00'
            if l00_files:
                acts.append(Act('loc', f'🎬 {tag} (特别篇清理 → 删本地)',
                                f'├─ 🎬 {tag}: 特别篇清理 ➔ CD2联动删除115网盘旧源',
                                l00_files,
                                meta={'reason': 'special_delete_local',
                                      'reason_label': '特别篇清理-删本地',
                                      'title': disp, 'season': 0}))
            if s00_files:
                acts.append(Act('shr', f'🎬 {tag} (特别篇清理 → 删分享)',
                                f'├─ 🎬 {tag}: 特别篇清理 ➔ 清理分享影视库strm',
                                s00_files,
                                meta={'reason': 'special_delete_share',
                                      'reason_label': '特别篇清理-删分享',
                                      'title': disp, 'season': 0}))
        elif special_action != 'ignore' and s00_files and l00_files:
            # 画质对比档
            s_q, l_q = _best(s00_files), _best(l00_files)
            tag = f'《{disp}》S00'
            if _share_wins(s_q, l_q):
                acts.append(Act('loc', f'🎬 {tag} (特别篇画质对比 → 删本地)',
                                f'├─ 🎬 {tag}: 分享画质达标 ➔ CD2联动删除115网盘旧源',
                                l00_files,
                                meta={'reason': 'special_share_better',
                                      'reason_label': '特别篇分享更优',
                                      'title': disp, 'season': 0}))
            else:
                acts.append(Act('shr', f'🎬 {tag} (特别篇画质对比 → 删分享)',
                                f'├─ 🎬 {tag}: 本地画质更优 ➔ 清理分享影视库strm',
                                s00_files,
                                meta={'reason': 'special_local_better',
                                      'reason_label': '特别篇本地更优',
                                      'title': disp, 'season': 0}))
        # ignore: 完全跳过

        s_proper = {k: v for k, v in s_seasons.items() if k > 0}
        l_proper = {k: v for k, v in l_seasons.items() if k > 0}
        n_local = len(l_proper)

        use_zero_sum = (s['decision'] == 'quality_first') and multi_protect and n_local >= 2

        if use_zero_sum:
            can_replace = True
            for sn, l_files in l_proper.items():
                if sn not in s_proper:
                    can_replace = False
                    break
                ok, _reason = _season_replaceable(s_proper[sn], l_files, disp)
                if not ok:
                    can_replace = False
                    break

            if can_replace:
                for sn, l_files in sorted(l_proper.items()):
                    tag = f'《{disp}》S{sn:02d}'
                    acts.append(Act('loc', f'📺 {tag} (整剧零和：分享全面达标 → 删本地)',
                                    f'├─ 📺 {tag}: 整剧零和 ➔ CD2联动删除115网盘旧源', l_files,
                                    meta={'reason': 'multi_zero_sum_share',
                                          'reason_label': '整剧零和-分享替代',
                                          'title': disp, 'season': sn,
                                          'local_seasons': n_local,
                                          'share_seasons': len(s_proper)}))
            else:
                for sn, s_files in sorted(s_proper.items()):
                    if sn in l_proper:
                        tag = f'《{disp}》S{sn:02d}'
                        acts.append(Act('shr', f'📺 {tag} (整剧零和：分享不达标 → 删分享)',
                                        f'├─ 📺 {tag}: 整剧零和 ➔ 清理分享影视库strm', s_files,
                                        meta={'reason': 'multi_zero_sum_local',
                                              'reason_label': '整剧零和-本地保留',
                                              'title': disp, 'season': sn,
                                              'local_seasons': n_local,
                                              'share_seasons': len(s_proper)}))
        else:
            for sn, s_files in sorted(s_proper.items()):
                tag = f'《{disp}》S{sn:02d}'
                s_eps = {_ep(x)[1] for x in s_files}

                if sn not in l_proper:
                    # 白皮书第4/8节：分享独有 Season（本地压根没有这季）默认保护，
                    # 不再因为"不完整/缺集"就把分享库里唯一的这份资源删掉——
                    # 两边都没有资源，用户还想再找的机会都没了。
                    # 之前这里在 not ok 时会生成 'shr' 删除 Action，现在统一改成 'keep' 仅报告。
                    ok, reason = _analyze(s_eps, disp)
                    if ok:
                        acts.append(Act('keep', f'🛡️ {tag} (分享独有，本地暂无此季，默认保护)',
                                        f'├─ 🛡️ {tag}: 分享独有 ➔ 默认保护，不自动清理',
                                        meta={'reason': 'share_only', 'reason_label': '分享独有-默认保护',
                                              'title': disp, 'season': sn}))
                    else:
                        acts.append(Act('keep', f'🛡️ {tag} (分享独有，{reason}，默认保护，不自动清理)',
                                        f'├─ 🛡️ {tag}: 分享独有 且 {reason} ➔ 默认保护，仅报告',
                                        meta={'reason': 'share_only_gap', 'reason_label': f'分享独有-{reason}',
                                              'title': disp, 'season': sn}))
                    continue

                l_files = l_proper[sn]
                l_eps = {_ep(x)[1] for x in l_files}
                s_q, l_q = _best(s_files), _best(l_files)

                if s_eps == l_eps:
                    ok, reason = _season_replaceable(s_files, l_files, disp)
                    if ok:
                        acts.append(Act('loc', f'📺 {tag} (逐集覆盖且质量达标，穿透删本地)',
                                        f'├─ 📺 {tag}: 逐集质量达标 ➔ CD2联动删除115网盘旧源', l_files,
                                        meta={'reason': 'season_all_pass',
                                              'reason_label': '逐集达标-删本地',
                                              'title': disp, 'season': sn}))
                    elif not _share_wins(_best(s_files), _best(l_files)):
                        acts.append(Act('shr', f'📺 {tag} (分享逐集画质不足，淘汰分享)',
                                        f'├─ 📺 {tag}: 分享画质次级 ➔ 清理分享影视库strm', s_files,
                                        meta={'reason': 'local_better',
                                              'reason_label': '本地画质更优',
                                              'title': disp, 'season': sn}))
                    else:
                        acts.append(Act('keep', f'📺 {tag} (逐集未全达标：{reason} → 保守双向保留)',
                                        f'├─ 🛡️ {tag}: {reason} ➔ 双向保留，不自动清理',
                                        meta={'reason': 'season_partial_pass',
                                              'reason_label': '部分集不达标-' + reason,
                                              'title': disp, 'season': sn}))
                elif s_eps >= l_eps and _is_seq(s_eps, disp):
                    ok, reason = _season_replaceable(s_files, l_files, disp)
                    if ok:
                        acts.append(Act('loc', f'📺 {tag} (分享更完整 {len(s_eps)}>{len(l_eps)}集 且逐集达标，剔除本地)',
                                        f'├─ 📺 {tag}: 分享更全({len(s_eps)}>{len(l_eps)}集)且逐集达标 ➔ CD2联动删除115网盘旧源', l_files,
                                        meta={'reason': 'share_more',
                                              'reason_label': '分享更完整',
                                              'title': disp, 'season': sn}))
                    else:
                        acts.append(Act('keep', f'📺 {tag} (分享领先 {len(s_eps)}>{len(l_eps)}集 但 {reason} → 双向保留)',
                                        f'├─ 🔄 {tag}: 分享领先但 {reason} ➔ 追更双向保留',
                                        meta={'reason': 'share_ahead_local_quality',
                                              'reason_label': '追更双向保留',
                                              'title': disp, 'season': sn,
                                              'local_quality': l_q, 'share_quality': s_q}))
                elif l_eps >= s_eps:
                    # 白皮书 §8：分享缺集时不再淘汰分享，避免"两边都不全"的资源彻底消失
                    acts.append(Act('keep', f'📺 {tag} (分享落后于本地 {len(l_eps)}>{len(s_eps)}集，保守双向保留)',
                                    f'├─ 🛡️ {tag}: 分享缺集 ➔ 双向保留，不自动清理',
                                    meta={'reason': 'share_behind',
                                          'reason_label': '分享缺集-双向保留',
                                          'title': disp, 'season': sn}))
                else:
                    acts.append(Act('shr', f'⚠️ {tag} (两库集数重叠错乱，淘汰分享)',
                                    f'├─ ⚠️ {tag}: 集数重叠错乱 ➔ 清理分享影视库strm', s_files,
                                    meta={'reason': 'overlap',
                                          'reason_label': '集数重叠错乱',
                                          'title': disp, 'season': sn}))
    return acts


def save_plan(acts):
    todo = [a for a in acts if a.kind not in ('keep', 'exempt') and a.action_id]
    if not todo:
        return None
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.time()
    ids = sorted(a.action_id for a in todo)
    pid = hashlib.md5(('\n'.join(ids) + str(ts)).encode()).hexdigest()[:8]
    actions_payload = []
    for a in todo:
        actions_payload.append({
            'action_id': a.action_id,
            'media_key': a.media_key,
            'kind': a.kind,
            'text': a.text,
            'reason': a.meta.get('reason', ''),
            'reason_label': a.meta.get('reason_label', ''),
            'title': a.meta.get('title', ''),
            'season': a.meta.get('season'),
            'files': [str(f) for f in a.files],
        })
    stats = {
        'loc': sum(1 for a in acts if a.kind == 'loc'),
        'shr': sum(1 for a in acts if a.kind == 'shr'),
        'keep': sum(1 for a in acts if a.kind == 'keep'),
        'exempt': sum(1 for a in acts if a.kind == 'exempt'),
    }
    payload = {
        'schema_version': 2,
        'id': pid,
        'ts': ts,
        'state': 'pending',
        'stats': stats,
        'actions': actions_payload,
        'executed_at': None,
        'executed_result': None,
    }
    (STATE_DIR / f'plan_{pid}.json').write_text(
        json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    return pid


def load_plan(plan_id):
    if not plan_id:
        return None
    safe = re.sub(r'[^0-9a-f]', '', str(plan_id))
    if not safe:
        return None
    pf = STATE_DIR / f'plan_{safe}.json'
    if not pf.exists():
        return None
    try:
        data = json.loads(pf.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if data.get('schema_version') != 2:
        return None
    return data


def save_plan_state(plan_id, state, extra=None):
    if not plan_id:
        return False
    safe = re.sub(r'[^0-9a-f]', '', str(plan_id))
    if not safe:
        return False
    pf = STATE_DIR / f'plan_{safe}.json'
    if not pf.exists():
        return False
    try:
        data = json.loads(pf.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    data['state'] = state
    if extra:
        data.update(extra)
    try:
        tmp = pf.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        tmp.replace(pf)
        return True
    except OSError:
        return False


def action_inter_check(args):
    acts = build_plan()

    # 扫描完成后顺便刷新 STRM 计数缓存（一次遍历，两份数据）
    try:
        _invalidate_lib_cache()
        S_now = Lib(S_ROOT)
        L_now = Lib(L_ROOT)
        _strm_count_cache.update({'ts': time.time(), 'local': L_now.strm_count, 'share': S_now.strm_count})
        log.info('扫描后刷新 STRM 计数: local=%d share=%d', L_now.strm_count, S_now.strm_count)
    except Exception as e:
        log.warning('刷新 STRM 计数缓存失败: %s', e)
    loc = [a for a in acts if a.kind == 'loc']
    shr = [a for a in acts if a.kind == 'shr']
    keep = [a for a in acts if a.kind == 'keep']
    exempted = [a for a in acts if a.kind == 'exempt']
    pid = save_plan(acts)
    write_audit_log('库间查重巡检',
                    f'扫描完成：待清理本地 {len(loc)} 项, 待清理分享 {len(shr)} 项, 保护 {len(keep)} 项, 豁免 {len(exempted)} 项')
    if loc or shr:
        notify_telegram(f'🔍 <b>双库扫描完成</b>\n待清理本地: {len(loc)}\n待淘汰分享: {len(shr)}\n受保护: {len(keep)}\n豁免: {len(exempted)}')
    return {
        'status': 'success', 'plan_id': pid,
        'total_clean_cnt': len(loc) + len(shr),
        'del_local_cnt': len(loc), 'del_share_cnt': len(shr),
        'del_local_items': [_act_to_dict(a) for a in loc],
        'del_share_items': [_act_to_dict(a) for a in shr],
        'protected_items': [_act_to_dict(a) for a in keep],
        'exempted_items':  [_act_to_dict(a) for a in exempted],
    }


def action_inter_clean(args):
    # 统一互斥锁：不管从 CLI、Web 任务队列还是 Telegram Bot 线程发起，
    # 只要是真正会修改文件的清理（非 dry-run），都必须先拿到这把跨入口的文件锁，
    # 避免三个入口各自维护自己的锁导致两个清理任务同时跑。
    lock = None
    if not args.dry_run:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        lock = open(LOCK_FILE, 'w')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            return {'status': 'busy', 'message': '已有治理任务正在执行，请稍后再试'}
    try:
        return _action_inter_clean_locked(args)
    finally:
        if lock:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()


def _action_inter_clean_locked(args):
    skipped_details = []
    if args.plan:
        plan = load_plan(args.plan)
        if plan is None:
            return {'status': 'error', 'code': 'plan_not_found',
                    'message': '清理计划不存在/已损坏（可能是旧格式），请重新诊断'}
        st = plan.get('state')
        if st in ('done', 'failed'):
            return {'status': 'error', 'code': 'plan_used',
                    'message': '清理计划已执行过（state=%s），不能重复执行' % st}
        if st == 'expired':
            return {'status': 'error', 'code': 'plan_expired',
                    'message': '清理计划已过期，请重新诊断'}
        if time.time() - plan.get('ts', 0) > PLAN_TTL:
            if not args.dry_run:
                save_plan_state(args.plan, 'expired')
            return {'status': 'error', 'code': 'plan_expired',
                    'message': '清理计划已超过 2 小时未执行，已过期，请重新诊断'}
        if not args.dry_run:
            save_plan_state(args.plan, 'executing')

        current = build_plan()
        current_by_id = {a.action_id: a for a in current
                         if a.kind not in ('keep', 'exempt') and a.action_id}
        old_actions = {act.get('action_id', ''): act
                       for act in plan.get('actions', []) if act.get('action_id')}
        todo = []
        for aid, old_act in old_actions.items():
            cur = current_by_id.get(aid)
            if cur is None:
                skipped_details.append(
                    '%s → 当前扫描已无此动作' % old_act.get('text', '?'))
                continue
            old_reason = old_act.get('reason', '')
            cur_reason = cur.meta.get('reason', '')
            if old_reason != cur_reason:
                skipped_details.append(
                    '%s → 决策原因变化: %s → %s' % (
                        old_act.get('text', '?'), old_reason, cur_reason))
                continue
            todo.append(cur)
        skipped = len(old_actions) - len(todo)
    else:
        todo = [a for a in build_plan() if a.kind not in ('keep', 'exempt')]
        skipped = 0

    n_loc = n_sh = 0
    detail, warns = [], []
    for a in todo:
        is_loc = a.kind == 'loc'
        r = safe_delete_files(a.files, L_ROOT if is_loc else S_ROOT, CLOUD_L_ROOT if is_loc else None, args.dry_run)
        line = ('[DRY-RUN] ' if args.dry_run else '') + a.detail
        if r['errors']:
            warns.append(f'{a.text}: ' + '; '.join(r['errors'][:2])); line += ' ⚠️ 部分失败'
        if r['strm_removed'] == 0 and not args.dry_run:
            detail.append(line + ' (未产生变更)'); continue
        if is_loc:
            _bits = []
            if r.get('cloud_missing'):   _bits.append(f'{r["cloud_missing"]} 个未找到源')
            if r.get('cloud_ambiguous'): _bits.append(f'{r["cloud_ambiguous"]} 个多候选')
            if r.get('cloud_fallback'):  _bits.append(f'{r["cloud_fallback"]} 个仅兜底')
            if _bits:
                line += ' ⚠️ ' + '、'.join(_bits)
        detail.append(line)
        n_loc += is_loc; n_sh += not is_loc

    refreshed = False
    if (n_loc or n_sh) and not args.dry_run:
        refreshed = notify_emby_refresh()
        # 文件已变动，主动失效 Lib 缓存，避免下次 build_plan 用到陈旧数据
        _invalidate_lib_cache()
        # 同时失效统计缓存（总览页下次重算）
        _strm_count_cache['ts'] = 0
        _lib_stats_cache['ts'] = 0
        _lib_stats_cache['data'] = None
    if skipped:
        detail.append(f'├─ ⏭ 有 {skipped} 项二次验证未通过，已跳过')
        for s in skipped_details[:5]:
            detail.append(f'│   · {s}')
    if not args.dry_run:
        purge_old()
        write_audit_log('执行跨库清理',
                        f'释放本地 {n_loc} 项, 淘汰分享 {n_sh} 项 (Emby刷新: {refreshed})',
                        detail + [f'⚠️ {w}' for w in warns])
        if n_loc or n_sh:
            notify_telegram(f'🗑️ <b>清理完成</b>\n释放本地: {n_loc}\n淘汰分享: {n_sh}\nEmby刷新: {"✅" if refreshed else "❌"}')
        if args.plan:
            save_plan_state(args.plan, 'done', {
                'executed_at': time.time(),
                'executed_result': {
                    'loc': n_loc, 'shr': n_sh,
                    'refreshed': refreshed,
                    'skipped': skipped,
                    'warnings': warns,
                },
            })
    return {'status': 'success', 'loc_cnt': n_loc, 'sh_cnt': n_sh, 'refreshed': refreshed,
            'detail': detail, 'warnings': warns, 'dry_run': args.dry_run,
            'skipped': skipped}


class TmdbError(Exception):
    pass


class Tmdb:
    def __init__(self):
        self.key = (RUNTIME_CFG.get('tmdb_key') or '').strip()
        self.cache_file = STATE_DIR / 'tmdb_cache.json'
        self.calls = self.hits = 0
        try:
            self.cache = json.loads(self.cache_file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            self.cache = {}

    def get(self, path, ttl=6 * 3600, **params):
        params.setdefault('language', TMDB_LANG)
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
        self.calls += 1
        time.sleep(0.03)
        return data

    def save(self):
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            cut = time.time() - 86400
            self.cache = {k: v for k, v in self.cache.items() if v['ts'] > cut}
            tmp = self.cache_file.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.cache, ensure_ascii=False), encoding='utf-8')
            tmp.replace(self.cache_file)
        except OSError as e:
            log.warning('TMDB 缓存写入失败: %s', e)


_emby_index_cache = {'ts': 0, 'data': None}


def emby_library_index(force=False):
    if not force and _emby_index_cache['data'] and time.time() - _emby_index_cache['ts'] < 300:
        return _emby_index_cache['data']
    out = {}
    for item_type in ('Movie', 'Series'):
        try:
            data = emby_request('/Items', {
                'Recursive': 'true', 'IncludeItemTypes': item_type,
                'Fields': 'ProviderIds,Path,ProductionYear,CommunityRating,ImageTags',
                'Limit': 50000,
            }) or {}
            for it in data.get('Items', []):
                tmdb_id = str((it.get('ProviderIds') or {}).get('Tmdb') or '')
                if not tmdb_id: continue
                out[tmdb_id] = {
                    'id': it.get('Id'), 'name': it.get('Name'), 'type': it.get('Type'),
                    'path': it.get('Path', '') or '', 'year': it.get('ProductionYear'),
                    'rating': it.get('CommunityRating'),
                    'has_image': 'Primary' in (it.get('ImageTags') or {}),
                }
        except Exception as e:
            log.warning('Emby 索引拉取失败 (%s): %s', item_type, e)
    _emby_index_cache['ts'] = time.time()
    _emby_index_cache['data'] = out
    return out


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
        tmdb_path = '/search/tv' if media == 'tv' else '/search/movie'
        params = {'query': query, 'page': page, 'include_adult': 'false'}
    else:
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
    cards = []
    for item in (res.get('results') or [])[:40]:
        tmdb_id = str(item.get('id', ''))
        title = item.get('title') or item.get('name') or ''
        date_str = item.get('release_date') or item.get('first_air_date') or ''
        year_str = date_str[:4] if date_str else ''
        rating = item.get('vote_average') or 0
        poster = item.get('poster_path')
        emby_hit = emby_index.get(tmdb_id)
        in_emby = emby_hit is not None
        in_local = in_emby and '影视媒体库' in (emby_hit.get('path') or '')
        in_share = in_emby and '分享影视库' in (emby_hit.get('path') or '')
        if poster: poster_url = f'{TMDB_IMG}{poster}'
        elif in_emby and emby_hit.get('has_image'): poster_url = f'/api/emby/poster/{emby_hit["id"]}'
        else: poster_url = ''
        cards.append({
            'tmdb_id': tmdb_id, 'title': title, 'year': year_str,
            'rating': round(rating, 1) if rating else None, 'poster': poster_url,
            'in_emby': in_emby, 'in_local': in_local, 'in_share': in_share,
            'emby_id': emby_hit['id'] if emby_hit else None,
            'type': 'tv' if media == 'tv' else 'movie',
        })

    t.save()
    return {'status': 'success', 'page': page,
            'total_pages': min(res.get('total_pages', 1), 20),
            'total_results': res.get('total_results', 0),
            'cards': cards, 'is_search': bool(query),
            'tmdb_calls': t.calls, 'tmdb_hits': t.hits}


_emby_lib_cache = {'ts': 0, 'data': None}


def emby_library_overview(force=False):
    if not force and _emby_lib_cache['data'] and time.time() - _emby_lib_cache['ts'] < 300:
        return _emby_lib_cache['data']
    out = {'series': [], 'movies': []}
    series_data = emby_request('/Items', {
        'Recursive': 'true', 'IncludeItemTypes': 'Series',
        'Fields': 'ProviderIds,Path,ProductionYear,CommunityRating,ImageTags,Genres',
        'Limit': 50000,
    }) or {}
    episodes_all = _fetch_all_episodes()
    eps_by_series = defaultdict(list)
    for ep in episodes_all:
        sid = ep.get('SeriesId')
        if sid: eps_by_series[sid].append(ep)

    for s in series_data.get('Items', []):
        sid = s.get('Id'); path = s.get('Path', '') or ''
        # Emby 有时会残留"空壳"剧集条目（元数据存在，但没有任何实际分集文件，
        # 常见于删除后 Emby 尚未彻底清理，或媒体库正在扫描中）。这类条目不该
        # 出现在片库映射对照里，否则用户会看到"Emby 没有数据"却仍被列出的剧。
        if not eps_by_series.get(sid):
            continue
        season_map = {}
        for ep in eps_by_series.get(sid, []):
            sn = ep.get('ParentIndexNumber'); en = ep.get('IndexNumber')
            if sn is None or en is None: continue
            season_map.setdefault(sn, set()).add(en)
        if not season_map:
            continue
        seasons = []
        total_missing = 0
        for sn in sorted(season_map.keys()):
            eps_set = sorted(season_map[sn])
            if not eps_set: continue
            lo, hi = eps_set[0], eps_set[-1]
            pre = list(range(1, lo)) if lo > 1 else []
            mid = sorted(set(range(lo, hi + 1)) - set(eps_set))
            miss = sorted(set(pre + mid))
            total_missing += len(miss)
            seasons.append({'season': sn, 'episodes': len(eps_set), 'max_ep': hi,
                            'missing': miss, 'complete': not miss})
        out['series'].append({
            'id': sid, 'name': s.get('Name'),
            'year': s.get('ProductionYear'), 'rating': s.get('CommunityRating'),
            'tmdb_id': (s.get('ProviderIds') or {}).get('Tmdb'),
            'genres': s.get('Genres', []),
            'in_local': '影视媒体库' in path, 'in_share': '分享影视库' in path, 'path': path,
            'has_image': 'Primary' in (s.get('ImageTags') or {}),
            'seasons': seasons, 'total_seasons': len(seasons),
            'total_episodes': sum(x['episodes'] for x in seasons),
            'missing_eps': total_missing,
            'complete': total_missing == 0 and len(seasons) > 0,
        })

    movie_data = emby_request('/Items', {
        'Recursive': 'true', 'IncludeItemTypes': 'Movie',
        'Fields': 'ProviderIds,Path,ProductionYear,CommunityRating,ImageTags,Genres',
        'Limit': 50000,
    }) or {}
    for m in movie_data.get('Items', []):
        path = m.get('Path', '') or ''
        out['movies'].append({
            'id': m.get('Id'), 'name': m.get('Name'),
            'year': m.get('ProductionYear'), 'rating': m.get('CommunityRating'),
            'tmdb_id': (m.get('ProviderIds') or {}).get('Tmdb'),
            'genres': m.get('Genres', []),
            'in_local': '影视媒体库' in path, 'in_share': '分享影视库' in path,
            'path': path, 'has_image': 'Primary' in (m.get('ImageTags') or {}),
        })
    _emby_lib_cache['ts'] = time.time()
    _emby_lib_cache['data'] = out
    return out


def classify_series_by_tmdb(local_seasons, tmdb_info):
    local_map = {s['season']: s['episodes'] for s in local_seasons if s['season'] > 0}
    local_total = sum(local_map.values())
    if not tmdb_info:
        return {'match_status': 'unmatched', 'tmdb_status': None,
                'local_total': local_total, 'tmdb_total': None, 'diff': None,
                'seasons': [{'season': sn, 'local': local_map[sn], 'tmdb': None, 'diff': None, 'status': 'unknown'} for sn in sorted(local_map)]}
    tmdb_status = tmdb_info.get('status', '') or ''
    tmdb_map = {}
    for s in (tmdb_info.get('seasons') or []):
        sn = s.get('season_number')
        if sn is None or sn <= 0: continue
        tmdb_map[sn] = s.get('episode_count', 0) or 0
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


def action_emby_library(args):
    force = bool(getattr(args, 'force', False))
    with_tmdb = getattr(args, 'with_tmdb', True)
    if with_tmdb is None: with_tmdb = True
    try:
        data = emby_library_overview(force=force)
    except Exception as e:
        return {'status': 'error', 'message': f'拉取 Emby 库失败: {e}'}
    series = data['series']; movies = data['movies']
    tmdb_errors = 0
    # 所有 series 先标 pending
    for s in series:
        s['tmdb_info'] = {'match_status': 'pending', 'tmdb_status': None,
                          'local_total': s.get('total_episodes', 0),
                          'tmdb_total': None, 'diff': None, 'seasons': []}

    if with_tmdb:
        t = Tmdb()
        has_key = bool(t.key)
        for s in series:
            tmdb_id = s.get('tmdb_id')
            if not tmdb_id:
                s['tmdb_info'] = {'match_status': 'no_tmdb' if has_key else 'unmatched',
                                  'tmdb_status': None, 'local_total': s.get('total_episodes', 0),
                                  'tmdb_total': None, 'diff': None, 'seasons': []}
                continue
            try:
                info = t.get(f'/tv/{tmdb_id}', ttl=TMDB_INFO_TTL)
            except TmdbError as e:
                log.warning('TMDB 查询失败 %s: %s', tmdb_id, e); info = None; tmdb_errors += 1
            except Exception as e:
                log.warning('TMDB 查询异常 %s: %s', tmdb_id, e); info = None; tmdb_errors += 1
            s['tmdb_info'] = classify_series_by_tmdb(s.get('seasons', []), info)
        t.save()
    stats = {'total_series': len(series), 'total_movies': len(movies),
             'aligned': 0, 'missing': 0, 'extra': 0, 'ongoing': 0, 'unmatched': 0, 'no_tmdb': 0,
             'complete_series': 0, 'incomplete_series': 0}
    for s in series:
        st = (s.get('tmdb_info') or {}).get('match_status', 'unmatched')
        if st in stats: stats[st] += 1
        if s.get('complete'): stats['complete_series'] += 1
        else: stats['incomplete_series'] += 1
    return {'status': 'success', 'stats': stats, 'series': series, 'movies': movies,
            'tmdb_errors': tmdb_errors, 'emby_host': EMBY_HOST}


# ═══════════════════ 入库监控（缓存层） ═══════════════════
def _fetch_ingest(hours=24):
    """实际拉取 Emby 近期入库"""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    tv_tree = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    mov_tree = defaultdict(lambda: defaultdict(list))
    movies_raw = []
    episodes_raw = []

    params_mov = {
        'Recursive': 'true', 'IncludeItemTypes': 'Movie',
        'Fields': 'DateCreated,Path,Genres',
        'SortBy': 'DateCreated', 'SortOrder': 'Descending',
        'Limit': 5000,
        'MinDateCreated': cutoff.strftime('%Y-%m-%dT%H:%M:%S.0000000Z'),
    }
    try:
        data = emby_request('/Items', params_mov) or {}
        for m in data.get('Items', []):
            dt = parse_dt(m.get('DateCreated'))
            if not dt or dt < cutoff: continue
            movies_raw.append(m)
            n = m.get('Name')
            bucket = mov_tree[_src(m.get('Path', ''))][parse_emby_library(m, True)]
            if n and n not in bucket: bucket.append(n)
    except Exception as e:
        log.warning('入库电影拉取失败: %s', e)

    params_ep = {
        'Recursive': 'true', 'IncludeItemTypes': 'Episode',
        'Fields': 'DateCreated,Path,SeriesName,Genres',
        'SortBy': 'DateCreated', 'SortOrder': 'Descending',
        'Limit': 5000,
        'MinDateCreated': cutoff.strftime('%Y-%m-%dT%H:%M:%S.0000000Z'),
    }
    try:
        data = emby_request('/Items', params_ep) or {}
        for e in data.get('Items', []):
            dt = parse_dt(e.get('DateCreated'))
            if not dt or dt < cutoff: continue
            episodes_raw.append(e)
            tv_tree[_src(e.get('Path', ''))][parse_emby_library(e, False)][e.get('SeriesName') or '未知剧集'] += 1
    except Exception as e:
        log.warning('入库剧集拉取失败: %s', e)

    total_mov = sum(len(v) for c in mov_tree.values() for v in c.values())
    total_series = sum(len(s) for c in tv_tree.values() for s in c.values())
    total_eps = sum(n for c in tv_tree.values() for s in c.values() for n in s.values())

    return {
        'ts': time.time(),
        'hours': hours,
        'stats': {'movies': total_mov, 'series': total_series, 'episodes': total_eps},
        'tree': {
            'tv': {k: {c: dict(s) for c, s in v.items()} for k, v in tv_tree.items()},
            'mov': {k: {c: list(ns) for c, ns in v.items()} for k, v in mov_tree.items()},
        },
        'movies_raw': [{'name': m.get('Name'), 'path': m.get('Path',''), 'created': m.get('DateCreated')} for m in movies_raw[:200]],
        'episodes_raw': [{'name': e.get('Name'), 'series': e.get('SeriesName'), 'path': e.get('Path',''), 'created': e.get('DateCreated')} for e in episodes_raw[:500]],
    }


def refresh_ingest_cache(hours=24) -> dict:
    """立即拉取，覆盖缓存"""
    log.info('入库缓存刷新开始（%sh）', hours)
    data = _fetch_ingest(hours=hours)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = INGEST_CACHE_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        tmp.replace(INGEST_CACHE_FILE)
        log.info('入库缓存刷新完成：电影 %d / 剧集 %d 部 / 集 %d',
                 data['stats']['movies'], data['stats']['series'], data['stats']['episodes'])
    except OSError as e:
        log.warning('入库缓存写入失败: %s', e)
    return data


def read_ingest_cache(max_age=None) -> dict:
    """读缓存；max_age 为 None 时不做时效判断，返回 None 表示无缓存"""
    try:
        data = json.loads(INGEST_CACHE_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if max_age is not None and time.time() - data.get('ts', 0) > max_age:
        return None
    return data


def get_ingest(hours=24, force_refresh=False) -> dict:
    """
    统一入口：
      force_refresh=True  → 立即拉最新
      force_refresh=False → 优先读缓存（10 分钟时效），过期则刷新
    """
    if force_refresh:
        return refresh_ingest_cache(hours=hours)
    cached = read_ingest_cache(max_age=600)
    if cached:
        cached['from_cache'] = True
        return cached
    data = refresh_ingest_cache(hours=hours)
    data['from_cache'] = False
    return data


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
    return '本地影视库' if '影视媒体库' in path else ('分享影视库' if '分享影视库' in path else '其它库')


def action_stats(args):
    """
    force_refresh kw: 'force' 时立即拉
    默认读缓存，无缓存刷新
    """
    kw = getattr(args, 'kw', '') or ''
    force = kw == 'force' or 'force' in kw
    full = 'full' in kw
    data = get_ingest(force_refresh=force)
    st = data.get('stats') or {}
    if not full:
        return {'status': 'success', 'has_more': True,
                'stats': st, 'from_cache': data.get('from_cache', False),
                'cache_ts': data.get('ts', 0),
                'text': '\n'.join([
                    '📊 **近 24 小时入库速报**', '━━━━━━━━━━━━━━━━━━━',
                    f'🎬 单片/电影新增：`+{st.get("movies", 0)}` 部',
                    f'📺 连载/剧集新增：`+{st.get("series", 0)}` 部共 `+{st.get("episodes", 0)}` 集'])}
    tree = data.get('tree') or {}
    tv_tree = tree.get('tv') or {}
    mov_tree = tree.get('mov') or {}
    rep = [f'📊 **24小时入库清单** (单片 `+{st.get("movies", 0)}` | 连载 `+{st.get("series", 0)}` 部 `+{st.get("episodes", 0)}` 集)', '━━━━━━━━━━━━━━━━━━━']
    rep.append(f'\n📺 **【连载剧集明细】** (共 {st.get("series", 0)} 部)')
    if st.get('series'):
        for src in ('本地影视库', '分享影视库'):
            for cat in sorted(tv_tree.get(src, {})):
                shows = tv_tree[src][cat]
                lines = [f'《{esc(n)}》`+{c}集`' for n, c in sorted(shows.items(), key=lambda x: x[1], reverse=True)]
                rep.append(f'┌ 📂 **{src} · {cat}** ({len(shows)}部)')
                rep += ['│  • ' + '  • '.join(lines[i:i + 2]) for i in range(0, len(lines), 2)]
                rep.append('└')
    else: rep.append('  • 暂无新增剧集')
    rep.append(f'\n🎬 **【单片电影明细】** (共 {st.get("movies", 0)} 部)')
    if st.get('movies'):
        for src in ('本地影视库', '分享影视库'):
            for cat in sorted(mov_tree.get(src, {})):
                names = mov_tree[src][cat]
                rep.append(f'┌ 📂 **{src} · {cat}** ({len(names)}部)')
                tags = [f'《{esc(n)}》' for n in names]
                rep += ['│  • ' + '、'.join(tags[i:i + 3]) for i in range(0, len(tags), 3)]
                rep.append('└')
    else: rep.append('  • 暂无新增电影')
    return {'status': 'success', 'text': '\n'.join(rep),
            'stats': st, 'tree': tree,
            'movies_raw': data.get('movies_raw', []),
            'episodes_raw': data.get('episodes_raw', []),
            'from_cache': data.get('from_cache', False),
            'cache_ts': data.get('ts', 0)}


def action_played(args):
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)
    newest = defaultdict(lambda: defaultdict(int))
    for ep in _recent('Episode', 'DateCreated,SeriesName,ParentIndexNumber,IndexNumber', 3000, cutoff):
        if ep.get('SeriesName') and ep.get('IndexNumber'):
            s = ep.get('ParentIndexNumber', 1)
            newest[ep['SeriesName']][s] = max(newest[ep['SeriesName']][s], ep['IndexNumber'])
    records, alerts = [], []
    def check(name, s_idx, e_idx, who, watched):
        top = newest.get(name, {}).get(s_idx, 0)
        if top > e_idx:
            verb = f'已看完 S{s_idx:02d}E{e_idx:02d}' if watched else f'看到 E{e_idx:02d}'
            a = {'user': who, 'series': name, 'watched': verb, 'new_ep': top,
                 'text': f'👤 {who} {verb}，《{name}》今日已入库 E{top:02d}'}
            if not any(x['user'] == who and x['series'] == name for x in alerts):
                alerts.append(a)
    for u in emby_request('/Users') or []:
        uid, who = u['Id'], u.get('Name', '用户')
        try:
            items = (emby_request(f'/Users/{uid}/Items', {'Recursive': 'true', 'Filters': 'IsResumable',
                                                          'SortBy': 'DatePlayed', 'SortOrder': 'Descending',
                                                          'Limit': 5}) or {}).get('Items', [])
            for it in items:
                name = it.get('SeriesName') or it.get('Name')
                ud = it.get('UserData', {})
                pct = max(1, min(99, int(ud.get('PlaybackPositionTicks', 0) / (it.get('RunTimeTicks') or 1) * 100)))
                if it.get('Type') == 'Movie':
                    records.append({'user': who, 'type': 'movie', 'name': name, 'pct': pct,
                                    'text': f'👤 {who} 正在看 🎬《{name}》 (进度 {pct}%)'})
                else:
                    s_idx, e_idx = it.get('ParentIndexNumber', 1), it.get('IndexNumber', 1)
                    records.append({'user': who, 'type': 'tv', 'name': name, 's': s_idx, 'e': e_idx, 'pct': pct,
                                    'text': f'👤 {who} 正在看 📺《{name}》S{s_idx:02d}E{e_idx:02d} (进度 {pct}%)'})
                    check(name, s_idx, e_idx, who, False)
            watched = (emby_request(f'/Users/{uid}/Items', {'Recursive': 'true', 'Filters': 'IsPlayed',
                                                            'SortBy': 'DatePlayed', 'SortOrder': 'Descending',
                                                            'Limit': 6, 'IncludeItemTypes': 'Episode'}) or {}).get('Items', [])
            for it in watched:
                if it.get('SeriesName'):
                    check(it['SeriesName'], it.get('ParentIndexNumber', 1), it.get('IndexNumber', 0), who, True)
        except Exception as e:
            log.warning('用户 %s 播放数据获取失败: %s', who, e)
    return {'status': 'success', 'records': records, 'alerts': alerts}


def action_search(args):
    tokens = [t for t in args.kw.casefold().split() if t]
    if not tokens:
        return {'status': 'success', 'text': '请输入片名关键词'}
    found = []
    for root, tag in ((L_ROOT, '本地影视库'), (S_ROOT, '分享影视库')):
        if not root.exists(): continue
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if parse_season_dir(d) is None]
            is_title = any(x.endswith('.strm') for x in fns) or any(parse_season_dir(d) is not None for d in os.listdir(dp) if os.path.isdir(os.path.join(dp, d)))
            base = os.path.basename(dp)
            if is_title and all(t in base.casefold() for t in tokens):
                p = Path(dp); rel = p.relative_to(root).parts
                m_type = rel[0] if rel else '影视库'; m_cat = rel[1] if len(rel) > 1 else '分类'
                icon = '🎬' if any(x in m_type for x in ('电影', '演唱会')) else '📺'
                found.append(f'{icon} **《{esc(p.name)}》**\n  ├ 📂 归属库: `{tag}`\n  ├ 🏷️ 分类: `{m_type} / {m_cat}`\n  └ 📍 路径: `{p}`')
    text = '\n\n'.join(found[:8]) if found else f'❌ 未在两库中检索到包含关键词《{esc(args.kw)}》的资源。'
    if len(found) > 8: text += f'\n\n… 共 {len(found)} 条，仅显示前 8 条，请补充关键词缩小范围'
    write_audit_log('模糊搜片', f'关键词: {args.kw}，命中 {len(found)} 条')
    return {'status': 'success', 'text': text}


def action_logs(args):
    n = int(args.kw) if args.kw.isdigit() else 35
    return {'status': 'success', 'text': logger.to_text(n)}

def _load_sub_state() -> dict:
    """读取订阅状态；文件不存在/损坏时返回空 dict"""
    try:
        return json.loads(SUB_STATE_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _save_sub_state(state: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SUB_STATE_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(SUB_STATE_FILE)


def _emby_series_latest_ep(series_tmdb_id: str):
    """查 Emby 里某剧（按 tmdb_id）的最新集"""
    if not series_tmdb_id: return None
    try:
        data = emby_request('/Items', {
            'Recursive': 'true', 'IncludeItemTypes': 'Series',
            'Fields': 'ProviderIds,Name', 'Limit': 1,
            'AnyProviderIdEquals': f'Tmdb.{series_tmdb_id}',
        }) or {}
    except Exception:
        data = {}
    items = data.get('Items') or []
    if not items:
        try:
            data = emby_request('/Items', {
                'Recursive': 'true', 'IncludeItemTypes': 'Series',
                'Fields': 'ProviderIds,Name', 'Limit': 50000,
            }) or {}
            for it in data.get('Items') or []:
                tid = str((it.get('ProviderIds') or {}).get('Tmdb') or '')
                if tid == str(series_tmdb_id):
                    items = [it]; break
        except Exception:
            return None
    if not items: return None
    series = items[0]
    sid = series.get('Id')
    sname = series.get('Name')
    try:
        eps = emby_request('/Items', {
            'ParentId': sid, 'Recursive': 'true', 'IncludeItemTypes': 'Episode',
            'Fields': 'ParentIndexNumber,IndexNumber,DateCreated',
            'SortBy': 'DateCreated', 'SortOrder': 'Descending', 'Limit': 1,
        }) or {}
    except Exception:
        return None
    ep_items = eps.get('Items') or []
    if not ep_items: return None
    ep = ep_items[0]
    return {
        'series_id': sid, 'series_name': sname,
        'season': ep.get('ParentIndexNumber') or 0,
        'episode': ep.get('IndexNumber') or 0,
        'date_created': ep.get('DateCreated') or '',
    }


def _tmdb_series_info(tmdb_id):
    """查 TMDB 已播集数、状态、季结构"""
    try:
        t = Tmdb()
        info = t.get(f'/tv/{tmdb_id}', ttl=TMDB_INFO_TTL)
        t.save()
        if not info: return None
        # 计算已播集（截止今天）
        today = datetime.date.today().isoformat()
        aired_seasons = {}
        for s in (info.get('seasons') or []):
            sn = s.get('season_number')
            if sn is None or sn <= 0: continue
            aired_seasons[sn] = {
                'episode_count': s.get('episode_count', 0) or 0,
                'air_date': s.get('air_date') or '',
            }
        return {
            'name': info.get('name'),
            'status': info.get('status', ''),
            'seasons': aired_seasons,
            'total_episodes': sum(v['episode_count'] for v in aired_seasons.values()),
        }
    except Exception as e:
        log.warning('TMDB 订阅查询失败 %s: %s', tmdb_id, e)
        return None


def _ep_key_num(k):
    m = re.match(r'S(\d+)E(\d+)', k or '')
    return (int(m.group(1)) * 10000 + int(m.group(2))) if m else 0


def check_subscriptions(send_notify=True) -> dict:
    """
    改进版：
      1. 拿 Emby 最新集（新集判定）
      2. 拿 TMDB 已播集（缺集判定）
      3. 双向提醒
    """
    cfg = _cfg.load_config()
    if cfg.get('subscribe_enabled', '1') != '1':
        return {'updates': [], 'skipped': 'disabled'}
    subs = _cfg.get_subscriptions()
    if not subs:
        return {'updates': [], 'total': 0}

    check_tmdb = cfg.get('subscribe_check_tmdb', '1') == '1'
    state = _load_sub_state()
    updates = []
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    for sub in subs:
        if not sub.get('enabled', True): continue
        sid = sub.get('id') or sub.get('tmdb_id') or sub.get('name')
        tmdb_id = sub.get('tmdb_id')
        if not tmdb_id: continue

        latest = _emby_series_latest_ep(tmdb_id)
        tmdb_info = _tmdb_series_info(tmdb_id) if check_tmdb else None

        # 无 Emby 数据且无 TMDB → 跳过
        if not latest and not tmdb_info: continue

        prev = state.get(sid) or {}
        prev_key = prev.get('latest_ep') or ''
        prev_tmdb_total = prev.get('tmdb_total') or 0

        cur_key = ''
        if latest:
            cur_key = f"S{latest['season']:02d}E{latest['episode']:02d}"

        new_ep_update = None
        missing_update = None

        # 新集判定
        if prev_key and cur_key and cur_key != prev_key:
            if _ep_key_num(cur_key) > _ep_key_num(prev_key):
                new_ep_update = {'old': prev_key, 'new': cur_key}

        # 缺集判定
        if tmdb_info and latest:
            tmdb_total = tmdb_info.get('total_episodes', 0) or 0
            # 本地总共 = Emby 现有
            emby_seasons = {latest['season']: latest['episode']}
            emby_total = sum(emby_seasons.values()) if emby_seasons else 0
            if tmdb_total > emby_total:
                missing_update = {
                    'tmdb_total': tmdb_total,
                    'emby_total': emby_total,
                    'diff': tmdb_total - emby_total,
                }

        state[sid] = {
            'tmdb_id': tmdb_id,
            'name': (latest and latest.get('series_name')) or (tmdb_info and tmdb_info.get('name')) or sub.get('name'),
            'latest_ep': cur_key or prev_key,
            'tmdb_total': (tmdb_info or {}).get('total_episodes', 0),
            'tmdb_status': (tmdb_info or {}).get('status', ''),
            'updated_at': now_str,
        }

        if new_ep_update or missing_update:
            updates.append({
                'name': state[sid]['name'],
                'tmdb_id': tmdb_id,
                'poster': sub.get('poster', ''),
                'new_ep': new_ep_update,
                'missing': missing_update,
            })

    _save_sub_state(state)

    if send_notify and updates:
        lines = ['🔔 <b>追更订阅</b>', '━━━━━━━━━━━━━━━━━━']
        for u in updates:
            lines.append(f"📺 《{u['name']}》")
            if u.get('new_ep'):
                lines.append(f"   🆕 新集入库 {u['new_ep']['old']} → <b>{u['new_ep']['new']}</b>")
            if u.get('missing'):
                m = u['missing']
                lines.append(f"   ⚠️ 缺 {m['diff']} 集（TMDB 已播 {m['tmdb_total']}）")
        notify_telegram('\n'.join(lines))

    return {'updates': updates, 'total': len(subs)}


# ═══════════════════ 晨报 ═══════════════════
def build_morning_report(items: list, force_refresh: bool = False) -> str:
    lines = [f'☀️ <b>TDD Guard 晨报</b> · {datetime.datetime.now().strftime("%Y-%m-%d")}',
             '━━━━━━━━━━━━━━━━━━']

    if 'stats' in items:
        try:
            # 晨报使用强制刷新（或读缓存）
            s_res = action_stats(argparse.Namespace(kw='force' if force_refresh else ''))
            st = s_res.get('stats') or {}
            lines.append('')
            lines.append('📊 <b>近 24h 入库</b>')
            lines.append(f"  🎬 电影 +{st.get('movies', 0)} 部")
            lines.append(f"  📺 剧集 +{st.get('series', 0)} 部 / +{st.get('episodes', 0)} 集")
        except Exception as e:
            lines.append(f'📊 入库统计失败: {e}')

    if 'subscriptions' in items:
        try:
            r = check_subscriptions(send_notify=False)
            lines.append('')
            lines.append(f"🔔 <b>订阅更新</b> ({r.get('total', 0)} 部订阅)")
            ups = r.get('updates') or []
            if ups:
                for u in ups[:10]:
                    if u.get('new_ep'):
                        lines.append(f"  📺 《{u['name']}》 {u['new_ep']['old']} → <b>{u['new_ep']['new']}</b>")
                    if u.get('missing'):
                        m = u['missing']
                        lines.append(f"  ⚠️ 《{u['name']}》缺 {m['diff']} 集")
            else:
                lines.append('  ✅ 无变化')
        except Exception as e:
            lines.append(f'🔔 订阅检查失败: {e}')

    if 'emby_gap' in items:
        try:
            data = emby_library_overview(force=False)
            broken = [s for s in data.get('series', []) if not s.get('complete')]
            lines.append('')
            lines.append(f"📺 <b>Emby 缺集</b> ({len(broken)} 部)")
            for s in broken[:8]:
                lines.append(f"  ⚠️ 《{s.get('name')}》缺 {s.get('missing_eps')} 集")
            if len(broken) > 8:
                lines.append(f"  … 共 {len(broken)} 部")
        except Exception as e:
            lines.append(f'📺 Emby 检查失败: {e}')

    return '\n'.join(lines)


def send_morning_report(items: list, force_refresh: bool = False) -> bool:
    """
    force_refresh=True → 立即扫描（晨报时间前预扫/手动测试用）
    """
    text = build_morning_report(items, force_refresh=force_refresh)
    ok = notify_telegram(text)
    if ok:
        _cfg.mark_morning_report_sent(datetime.date.today().isoformat())
    return ok


def patch_emby_lib_cache_after_series_delete(series_id: str, deleted_target: str):
    """单剧删除后，就地更新内存 + 磁盘上的片库映射缓存（含 TMDB 对照结果），
    而不是整体清空——这样片库映射页不需要为了看到最新状态而重新跑一遍
    很慢的全量 TMDB 对照，其它剧集的对照结果也不会被打回"待对照"。"""
    for cache in (_emby_lib_cache, {'data': read_emby_lib_cache()}):
        data = cache.get('data')
        if not data:
            continue
        series_list = data.get('series') or []
        idx = next((i for i, s in enumerate(series_list) if s.get('id') == series_id), None)
        if idx is None:
            continue
        remaining = (emby_request('/Items', {
            'ParentId': series_id, 'Recursive': 'true',
            'IncludeItemTypes': 'Episode', 'Fields': 'Path', 'Limit': 5000,
        }) or {}).get('Items') or []
        if not remaining:
            series_list.pop(idx)
        else:
            paths = [emby_path_to_container(e.get('Path') or '') for e in remaining]
            series_list[idx]['in_local'] = any(p and _inside(p, L_ROOT) for p in paths)
            series_list[idx]['in_share'] = any(p and _inside(p, S_ROOT) for p in paths)
        data['series'] = series_list
        if cache is _emby_lib_cache:
            _emby_lib_cache['data'] = data
            _emby_lib_cache['ts'] = time.time()
        else:
            save_emby_lib_cache(data)


def save_emby_lib_cache(data: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    data['ts'] = time.time()
    tmp = EMBY_LIB_CACHE_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    tmp.replace(EMBY_LIB_CACHE_FILE)


def read_emby_lib_cache(max_age=None):
    try:
        data = json.loads(EMBY_LIB_CACHE_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if max_age is not None and time.time() - data.get('ts', 0) > max_age:
        return None
    return data


_tmdb_scan_progress = {
    'running': False, 'total': 0, 'done': 0, 'stage': '',
    'started_at': 0, 'finished_at': 0, 'stats': {}, 'error': '',
}


def get_tmdb_scan_progress() -> dict:
    p = dict(_tmdb_scan_progress)
    if p['running'] and p['started_at']:
        p['elapsed_sec'] = int(time.time() - p['started_at'])
        p['eta_sec'] = int(p['elapsed_sec'] / p['done'] * (p['total'] - p['done'])) if p['done'] > 0 else None
        p['percent'] = int(p['done'] / p['total'] * 100) if p['total'] else 0
    else:
        p['elapsed_sec'] = (p['finished_at'] - p['started_at']) if p['finished_at'] and p['started_at'] else 0
        p['eta_sec'] = None
        p['percent'] = 100 if p['finished_at'] else 0
    return p


def refresh_tmdb_scan():
    """后台跑一次完整 TMDB 对照并落盘（带进度上报）"""
    global _tmdb_scan_progress
    log.info('TMDB 对照开始（后台）')
    _tmdb_scan_progress.update({
        'running': True, 'total': 0, 'done': 0,
        'stage': '拉取 Emby 库...',
        'started_at': time.time(), 'finished_at': 0,
        'stats': {}, 'error': '',
    })
    try:
        data = emby_library_overview(force=True)
        series = data['series']
        movies = data['movies']
        total = len(series)
        _tmdb_scan_progress['total'] = total
        _tmdb_scan_progress['stage'] = f'对照 TMDB（共 {total} 部）...'
        t = Tmdb()
        has_key = bool(t.key)
        tmdb_errors = 0
        for i, s_ in enumerate(series):
            tmdb_id = s_.get('tmdb_id')
            if not tmdb_id:
                s_['tmdb_info'] = {'match_status': 'no_tmdb' if has_key else 'unmatched',
                                   'tmdb_status': None,
                                   'local_total': s_.get('total_episodes', 0),
                                   'tmdb_total': None, 'diff': None, 'seasons': []}
            else:
                try:
                    info = t.get(f'/tv/{tmdb_id}', ttl=TMDB_INFO_TTL)
                except TmdbError as e:
                    log.warning('TMDB 查询失败 %s: %s', tmdb_id, e)
                    info = None; tmdb_errors += 1
                except Exception as e:
                    log.warning('TMDB 查询异常 %s: %s', tmdb_id, e)
                    info = None; tmdb_errors += 1
                s_['tmdb_info'] = classify_series_by_tmdb(s_.get('seasons', []), info)
            _tmdb_scan_progress['done'] = i + 1
            if (i + 1) % 20 == 0 or (i + 1) == total:
                el = time.time() - _tmdb_scan_progress['started_at']
                eta = el / (i + 1) * (total - i - 1)
                log.info('TMDB 进度 %d/%d (%.0f%%) ETA %.0f 秒', i + 1, total,
                         (i + 1) / total * 100 if total else 100, eta)
        t.save()
        stats = {'total_series': len(series), 'total_movies': len(movies),
                 'aligned': 0, 'missing': 0, 'extra': 0, 'ongoing': 0,
                 'unmatched': 0, 'no_tmdb': 0, 'pending': 0,
                 'complete_series': 0, 'incomplete_series': 0}
        for s_ in series:
            st = (s_.get('tmdb_info') or {}).get('match_status', 'unmatched')
            if st in stats: stats[st] += 1
            if s_.get('complete'): stats['complete_series'] += 1
            else: stats['incomplete_series'] += 1
        res = {'status': 'success', 'stats': stats, 'series': series, 'movies': movies,
               'tmdb_errors': tmdb_errors, 'with_tmdb': True, 'emby_host': EMBY_HOST}
        save_emby_lib_cache(res)
        cfg = _cfg.load_config()
        cfg['tmdb_scan_last_ts'] = str(time.time())
        _cfg.save_config(cfg)
        _tmdb_scan_progress.update({'running': False, 'finished_at': time.time(),
                                    'stage': '完成', 'stats': stats})
        log.info('TMDB 对照完成：对齐 %d / 缺集 %d / 超集 %d / 在更 %d',
                 stats['aligned'], stats['missing'], stats['extra'], stats['ongoing'])
        return res
    except Exception as e:
        log.exception('TMDB 对照失败')
        _tmdb_scan_progress.update({'running': False, 'finished_at': time.time(),
                                    'stage': '失败', 'error': str(e)})
        return {'status': 'error', 'message': str(e)}


def scan_exempt_matches():
    """扫描双库，返回命中白名单的条目"""
    kws = _exempt_keywords()
    if not kws:
        return []
    results = {}
    for root, lib_name in ((L_ROOT, '本地'), (S_ROOT, '分享')):
        if not root.exists():
            continue
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            hits = [k for k in kws if k in entry.name]
            if not hits:
                continue
            seasons = sorted(
                s for s in (parse_season_dir(d.name) for d in entry.iterdir()
                            if d.is_dir()) if s is not None
            )
            r = results.setdefault(entry.name,
                {'title': entry.name, 'keyword': hits[0], 'libs': [], 'seasons': []})
            r['libs'].append(lib_name)
            r['seasons'] = sorted(set(r['seasons']) | set(seasons))
    return list(results.values())



def action_library_stats(args):
    # 缓存命中则直接返回
    now = time.time()
    if _lib_stats_cache['data'] is not None and (now - _lib_stats_cache['ts']) < _CACHE_TTL:
        return _lib_stats_cache['data']
    """统计双库各分类的 STRM 数量（按实际目录名匹配）"""
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

    for root, key in ((L_ROOT, 'local'), (S_ROOT, 'share')):
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
    _lib_stats_cache['ts'] = time.time()
    _lib_stats_cache['data'] = result
    return result



def emby_path_to_container(emby_path):
    """将 Emby 返回的 Path 转成容器内路径"""
    if not emby_path:
        return None
    p = str(emby_path)
    pre_l = '/strm/115网盘/影视媒体库/'
    pre_s = '/strm/115网盘/分享影视库/'
    if p.startswith(pre_l):
        return L_ROOT / p[len(pre_l):]
    if p.startswith(pre_s):
        return S_ROOT / p[len(pre_s):]
    return None



# ============ 性能缓存（5 分钟 TTL）============
_strm_count_cache = {'ts': 0, 'local': 0, 'share': 0}
_lib_stats_cache = {'ts': 0, 'data': None}
_CACHE_TTL = 300


def _get_strm_counts():
    """缓存 STRM 总数（5 分钟）"""
    now = time.time()
    if now - _strm_count_cache['ts'] < _CACHE_TTL and _strm_count_cache['ts'] > 0:
        return _strm_count_cache['local'], _strm_count_cache['share']
    l = sum(1 for _ in L_ROOT.rglob('*.strm')) if L_ROOT.exists() else 0
    s_ = sum(1 for _ in S_ROOT.rglob('*.strm')) if S_ROOT.exists() else 0
    _strm_count_cache.update({'ts': now, 'local': l, 'share': s_})
    log.info('缓存刷新 STRM 计数: local=%d share=%d', l, s_)
    return l, s_


def invalidate_stats_cache():
    """清空统计缓存（删除后调用）"""
    _strm_count_cache['ts'] = 0
    _lib_stats_cache['ts'] = 0
    _lib_stats_cache['data'] = None


ACTIONS = {
    'inter_check':  action_inter_check,
    'inter_clean':  action_inter_clean,
    'stats':        action_stats,
    'played':       action_played,
    'search':       action_search,
    'logs':         action_logs,
    'explore':      action_explore,
    'emby_library': action_emby_library,
    'library_stats': action_library_stats,
    'orphans':       action_scan_orphans,
}
MUTATING = {'inter_clean'}


def main():

    ap = argparse.ArgumentParser()
    ap.add_argument('--action', required=True, choices=list(ACTIONS))
    ap.add_argument('--kw', default='')
    ap.add_argument('--plan', default='')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    # 互斥锁现在统一由 action_inter_clean 自己持有（见该函数），
    # 这样 CLI / Web / Telegram Bot 三个入口共用同一把锁，这里不再重复加锁。
    try:
        res = ACTIONS[args.action](args)
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        res = {'status': 'error', 'message': f'{type(e).__name__}: {e}'}
    print(json.dumps(res, ensure_ascii=False))


if __name__ == '__main__':
    main()