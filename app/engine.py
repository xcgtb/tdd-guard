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

L_ROOT = _p('L_ROOT', '/vol1/1000/TgtoDrive/strm/115网盘/影视媒体库')
S_ROOT = _p('S_ROOT', '/vol1/1000/TgtoDrive/strm/115网盘/分享影视库')
CLOUD_L_ROOT = _p('CLOUD_L_ROOT', '/vol1/1000/docker/clouddrive2/CloudDrive/影视媒体库')
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
    return _strategy().get('special_action', 'keep')

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
    if decision == 'balanced':   return s_q > l_q
    return s_q > l_q if s['tie_keep_local'] else s_q >= l_q


class Busy(Exception):
    pass


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
        if not root.exists(): return
        for f in root.rglob('*.strm'):
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
    """在 parent 下找名字最接近 target_name 的子目录"""
    from pathlib import Path as _P
    if not parent.is_dir(): return None
    target_norm = _normalize_title(target_name)
    if not target_norm: return None
    exact = parent / target_name
    if exact.is_dir(): return exact
    for child in parent.iterdir():
        if child.is_dir() and _normalize_title(child.name) == target_norm:
            return child
    for child in parent.iterdir():
        if not child.is_dir(): continue
        cn = _normalize_title(child.name)
        if cn and (cn.startswith(target_norm) or target_norm.startswith(cn)):
            return child
    return None

def cloud_videos(f, base_root, cloud_root):
    """查找 strm 对应的云端视频文件（目录名 + 文件名双模糊匹配 + 目录唯一兜底）"""
    try:
        rel_parent = f.relative_to(base_root).parent
    except ValueError:
        return []
    d = cloud_root
    for part in rel_parent.parts:
        if not d.is_dir(): return []
        next_d = d / part
        if next_d.is_dir():
            d = next_d
        else:
            next_d = _find_dir_fuzzy(d, part)
            if not next_d: return []
            d = next_d
    if not d.is_dir(): return []
    out, stem = [], f.stem
    if Path(stem).suffix.lower() in VIDEO_EXTS and (d / stem).is_file():
        out.append(d / stem)
    for ext in VIDEO_EXTS:
        c = d / f'{stem}{ext}'
        if c.is_file() and c not in out:
            out.append(c)
    if not out:
        stem_norm = _normalize_title(stem)
        for child in d.iterdir():
            if not child.is_file(): continue
            if child.suffix.lower() not in VIDEO_EXTS: continue
            if _normalize_title(child.stem) == stem_norm:
                out.append(child)
    # 兜底：目录里只有一个视频文件时，认为就是它
    if not out:
        try:
            vids = [c for c in d.iterdir()
                    if c.is_file() and c.suffix.lower() in VIDEO_EXTS]
            if len(vids) == 1:
                out.append(vids[0])
        except OSError:
            pass
    return out


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


def _prune_up(d, base_root, cloud_root):
    while d != base_root:
        try: rel = d.relative_to(base_root)
        except ValueError: return
        if len(rel.parts) < PRUNE_MIN_DEPTH: return
        if d.exists():
            if any(d.rglob('*.strm')): return
            shutil.rmtree(d, ignore_errors=True)
        if cloud_root:
            try: (cloud_root / rel).rmdir()
            except OSError: pass
        d = d.parent


def safe_delete_files(files, base_root, cloud_root=None, dry_run=False):
    st = {'strm_removed': 0, 'cloud_removed': 0, 'cloud_missing': 0, 'errors': []}
    parents = set()
    for f in files:
        if f.suffix.lower() != '.strm' or not _inside(f, base_root):
            st['errors'].append(f'跳过非法路径: {f}')
            continue
        vids = cloud_videos(f, base_root, cloud_root) if cloud_root and cloud_root.exists() else []
        if cloud_root and not vids:
            st['cloud_missing'] += 1
        if dry_run:
            st['strm_removed'] += 1
            st['cloud_removed'] += len(vids)
            continue
        ok = True
        for v in vids:
            try:
                v.unlink(); st['cloud_removed'] += 1
            except OSError as e:
                ok = False; st['errors'].append(f'{v.name}: {e}')
        if not ok: continue
        try:
            _remove_strm(f, base_root); st['strm_removed'] += 1
            parents.add(f.parent)
        except OSError as e:
            st['errors'].append(f'{f.name}: {e}')
    for p in sorted(parents, key=lambda x: len(x.parts), reverse=True):
        _prune_up(p, base_root, cloud_root)
    return st


def purge_old():
    cut = time.time() - TRASH_KEEP_DAYS * 86400
    if TRASH_DIR.exists():
        for d in TRASH_DIR.iterdir():
            try:
                if d.is_dir() and d.stat().st_mtime < cut:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError: pass
    if STATE_DIR.exists():
        for f in STATE_DIR.glob('plan_*.json'):
            try:
                if f.stat().st_mtime < time.time() - 86400: f.unlink()
            except OSError: pass


@dataclass
class Act:
    kind: str
    text: str
    detail: str
    files: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    @property
    def key(self):
        return f'{self.kind}|{self.text}'


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
    # 用 _get_lib 而不是直接 Lib(...)：30 秒内多次 build_plan 复用同一份数据。
    # 典型场景：扫描 → 立刻执行清理，不重复 rglob 全库。
    S, L = _get_lib(S_ROOT), _get_lib(L_ROOT)
    s = _strategy()
    multi_protect = s['multi_season_protect']
    special_action = s.get('special_action', 'keep')
    acts = []

    for key, s_files in S.mov.items():
        meta = (key, S.meta[key])
        lk = L.find(meta, L.mov)
        if not lk: continue
        disp, l_files = S.meta[key][0], L.mov[lk]
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
        n_local = len(l_seasons)
        multi = n_local >= 2 and len(s_seasons) < n_local

        for s_num, s_files in sorted(s_seasons.items()):
            tag = f'《{disp}》S{s_num:02d}'
            s_eps = {_ep(x)[1] for x in s_files}

            # ─── 特别篇 S00 特殊处理 ───
            if s_num == 0:
                if special_action == 'ignore':
                    continue    # 忽略，不参与治理
                elif special_action == 'delete':
                    # 强制清理
                    acts.append(Act('shr', f'🎬 {tag} (特别篇 · 按策略清理)',
                                    f'├─ 🎬 {tag}: 特别篇策略=清理 ➔ 淘汰分享影视库strm', s_files,
                                    meta={'reason': 'special_delete', 'reason_label': '特别篇清理',
                                          'title': disp, 'season': 0}))
                    continue
                # keep: 默认保留（走白名单豁免）
                acts.append(Act('exempt', f'🛡️ {tag} (特别篇 · 保留)',
                                f'├─ 🛡️ {tag}: 特别篇策略=保留 ➔ 跳过清理',
                                s_files,
                                meta={'reason': 'special_keep', 'reason_label': '特别篇保留',
                                      'title': disp, 'season': 0, 'keywords': ['特别篇']}))
                continue

            if s_num not in l_seasons:
                hit_kws = _exempt_hit(disp)
                if not hit_kws:
                    for f in s_files:
                        hit_kws = _exempt_hit(str(f))
                        if hit_kws: break
                if hit_kws:
                    acts.append(Act('exempt', f'🛡️ {tag} (白名单豁免)',
                                    f'├─ 🛡️ {tag}: 命中白名单 [{",".join(hit_kws)}] ➔ 跳过清理',
                                    s_files,
                                    meta={'reason': 'whitelist', 'reason_label': '白名单豁免',
                                          'title': disp, 'season': s_num, 'keywords': hit_kws}))
                    continue
                ok, reason = _analyze(s_eps, disp)
                if not ok:
                    acts.append(Act('shr', f'🚫 {tag} ({reason})', f'├─ 🚫 {tag}: {reason} ➔ 清理分享影视库strm', s_files,
                                    meta={'reason': 'gap', 'reason_label': reason, 'title': disp, 'season': s_num}))
                continue

            l_files = l_seasons[s_num]
            l_eps = {_ep(x)[1] for x in l_files}
            s_q, l_q = _best(s_files), _best(l_files)

            if multi and multi_protect:
                if s_q > l_q:
                    acts.append(Act('keep', f'📺 {tag} (本地拥有{n_local}季完整合集，保护本地合集不被掏空)',
                                    f'├─ 🛡️ {tag}: 本地{n_local}季大合集保护 ➔ 保持本地不删',
                                    meta={'reason': 'multi_season', 'reason_label': '多季合集保护',
                                          'title': disp, 'season': s_num,
                                          'local_seasons': n_local, 'share_seasons': len(s_seasons),
                                          'local_quality': l_q, 'share_quality': s_q}))
                else:
                    acts.append(Act('shr', f'📺 {tag} (本地已拥有{n_local}季大合集，淘汰零碎单季分享)',
                                    f'├─ 📺 {tag}: 本地已有合集 ➔ 清理分享影视库strm', s_files,
                                    meta={'reason': 'local_collection', 'reason_label': '本地合集优先',
                                          'title': disp, 'season': s_num, 'local_seasons': n_local}))
                continue

            if s_eps == l_eps:
                if _share_wins(s_q, l_q):
                    acts.append(Act('loc', f'📺 {tag} (集数对齐，分享画质达标，穿透删本地)',
                                    f'├─ 📺 {tag}: 画质达标且对齐 ➔ CD2联动删除115网盘旧源', l_files,
                                    meta={'reason': 'share_better', 'reason_label': '分享画质达标且对齐',
                                          'title': disp, 'season': s_num}))
                else:
                    acts.append(Act('shr', f'📺 {tag} (集数对齐，但分享画质较差，淘汰分享)',
                                    f'├─ 📺 {tag}: 分享画质次级 ➔ 清理分享影视库strm', s_files,
                                    meta={'reason': 'local_better', 'reason_label': '本地画质更优',
                                          'title': disp, 'season': s_num}))
            elif s_eps >= l_eps and _is_seq(s_eps, disp):
                if _share_wins(s_q, l_q):
                    acts.append(Act('loc', f'📺 {tag} (分享库更完整 {len(s_eps)}>{len(l_eps)}集 且画质达标，剔除本地残缺源)',
                                    f'├─ 📺 {tag}: 分享更全({len(s_eps)}>{len(l_eps)}集)且画质达标 ➔ CD2联动删除115网盘旧源', l_files,
                                    meta={'reason': 'share_more', 'reason_label': '分享更完整',
                                          'title': disp, 'season': s_num}))
                else:
                    acts.append(Act('keep', f'📺 {tag} (分享领先 {len(s_eps)}>{len(l_eps)}集，但本地画质高，双向保留)',
                                    f'├─ 🔄 {tag}: 分享领先但本地画质高 ➔ 追更双向保留',
                                    meta={'reason': 'share_ahead_local_quality', 'reason_label': '追更双向保留',
                                          'title': disp, 'season': s_num,
                                          'local_quality': l_q, 'share_quality': s_q}))
            elif l_eps >= s_eps:
                acts.append(Act('shr', f'📺 {tag} (分享落后于本地库，淘汰分享)',
                                f'├─ 📺 {tag}: 分享落后于本地 ➔ 清理分享影视库strm', s_files,
                                meta={'reason': 'share_behind', 'reason_label': '分享落后',
                                      'title': disp, 'season': s_num}))
            else:
                acts.append(Act('shr', f'⚠️ {tag} (两库集数重叠错乱，淘汰分享)',
                                f'├─ ⚠️ {tag}: 集数重叠错乱 ➔ 清理分享影视库strm', s_files,
                                meta={'reason': 'overlap', 'reason_label': '集数重叠错乱',
                                      'title': disp, 'season': s_num}))
    return acts


def save_plan(acts):
    keys = sorted(a.key for a in acts if a.kind not in ('keep', 'exempt'))
    if not keys: return None
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.time()
    pid = hashlib.md5(('\n'.join(keys) + str(ts)).encode()).hexdigest()[:8]
    (STATE_DIR / f'plan_{pid}.json').write_text(
        json.dumps({'id': pid, 'ts': ts, 'keys': keys}, ensure_ascii=False), encoding='utf-8')
    return pid


def action_inter_check(args):
    acts = build_plan()
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
    if args.plan:
        pf = STATE_DIR / f'plan_{re.sub(r"[^0-9a-f]", "", args.plan)}.json'
        if not pf.exists():
            return {'status': 'error', 'code': 'plan_not_found', 'message': '清理计划不存在或已被使用，请重新诊断'}
        plan = json.loads(pf.read_text(encoding='utf-8'))
        if time.time() - plan['ts'] > PLAN_TTL:
            pf.unlink(missing_ok=True)
            return {'status': 'error', 'code': 'plan_expired', 'message': '清理计划已过期，请重新诊断'}
        if not args.dry_run: pf.unlink(missing_ok=True)
        allowed = set(plan['keys'])
        todo = [a for a in build_plan() if a.kind not in ('keep', 'exempt') and a.key in allowed]
        skipped = len(allowed) - len(todo)
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
        if is_loc and r['cloud_missing']:
            line += f' ⚠️ {r["cloud_missing"]} 个 strm 未找到对应115实体'
        detail.append(line)
        n_loc += is_loc; n_sh += not is_loc

    refreshed = False
    if (n_loc or n_sh) and not args.dry_run:
        refreshed = notify_emby_refresh()
        # 文件已变动，主动失效 Lib 缓存，避免下次 build_plan 用到陈旧数据
        _invalidate_lib_cache()
    if skipped: detail.append(f'├─ ⏭ 有 {skipped} 项自诊断后状态已变化，已跳过')
    if not args.dry_run:
        purge_old()
        write_audit_log('执行跨库清理', f'释放本地 {n_loc} 项, 淘汰分享 {n_sh} 项 (Emby刷新: {refreshed})',
                        detail + [f'⚠️ {w}' for w in warns])
        if n_loc or n_sh:
            notify_telegram(f'🗑️ <b>清理完成</b>\n释放本地: {n_loc}\n淘汰分享: {n_sh}\nEmby刷新: {"✅" if refreshed else "❌"}')
    return {'status': 'success', 'loc_cnt': n_loc, 'sh_cnt': n_sh, 'refreshed': refreshed,
            'detail': detail, 'warnings': warns, 'dry_run': args.dry_run}


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
    '动作': {'movie': 28,    'tv': 10759},
    '喜剧': {'movie': 35,    'tv': 35},
    '犯罪': {'movie': 80,    'tv': 80},
    '纪录': {'movie': 99,    'tv': 99},
    '剧情': {'movie': 18,    'tv': 18},
    '动画': {'movie': 16,    'tv': 16},
    '悬疑': {'movie': 9648,  'tv': 9648},
    '科幻': {'movie': 878,   'tv': 10765},
    '恐怖': {'movie': 27,    'tv': None},
    '战争': {'movie': 10752, 'tv': 10768},
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

    try:
        res = t.get(tmdb_path, **params) or {}
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
        season_map = {}
        for ep in eps_by_series.get(sid, []):
            sn = ep.get('ParentIndexNumber'); en = ep.get('IndexNumber')
            if sn is None or en is None: continue
            season_map.setdefault(sn, set()).add(en)
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
    return {
        'status': 'success',
        'rows': rows,
        'local_total': out['local_total'],
        'share_total': out['share_total'],
        'local_other': out['local_other'],
        'share_other': out['share_other'],
    }


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
}
MUTATING = {'inter_clean'}


def main():

    ap = argparse.ArgumentParser()
    ap.add_argument('--action', required=True, choices=list(ACTIONS))
    ap.add_argument('--kw', default='')
    ap.add_argument('--plan', default='')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    lock = None
    try:
        if args.action in MUTATING and not args.dry_run:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            lock = open(LOCK_FILE, 'w')
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise Busy()
        res = ACTIONS[args.action](args)
    except Busy:
        res = {'status': 'busy', 'message': '已有治理任务正在执行，请稍后再试'}
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        res = {'status': 'error', 'message': f'{type(e).__name__}: {e}'}
    finally:
        if lock: lock.close()
    print(json.dumps(res, ensure_ascii=False))


if __name__ == '__main__':
    main()