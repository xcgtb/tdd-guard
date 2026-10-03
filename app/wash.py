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
from . import state, governance, emby, lib, storage

log = logging.getLogger('media_agent')


VIDEO_EXTS = ['.mkv', '.mp4', '.ts', '.mov', '.iso', '.m2ts']
_METADATA_EXTS = {
    '.nfo', '.jpg', '.jpeg', '.png', '.gif', '.webp',
    '.srt', '.ass', '.ssa', '.sub', '.idx', '.sup',
    '.json', '.url', '.xml', '.md5', '.sha1',
}
PRUNE_MIN_DEPTH = int(os.environ.get('PRUNE_MIN_DEPTH', '3'))
_SIDECAR_SEPS = '.-_ [('
_RE_DIR_TMDB = re.compile(r'(?i)tmdb(?:id)?[-_=: ]*(\d+)')
_ORPHAN_IGNORE_NAMES = {'thumbs.db', 'desktop.ini', '.ds_store'}


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
        sub, level = lib._find_dir_fuzzy(d, part)
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
    stem_norm = lib._normalize_title(stem)
    norm_matches = []
    try:
        for child in d.iterdir():
            if not child.is_file(): continue
            if child.suffix.lower() not in VIDEO_EXTS: continue
            if not _inside(child, cloud_root): continue
            if lib._normalize_title(child.stem) == stem_norm:
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

class MutationBusy(Exception):
    pass

@contextlib.contextmanager
def mutation_lock():
    """跨入口互斥锁：CLI / Web / Telegram Bot 任何真正改文件的操作（双库清理、单剧删除、
    洗版残留清理）都必须先拿到这把文件锁，拿不到抛 MutationBusy，由调用方回「忙」。"""
    state.DATA_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(state.LOCK_FILE, 'w')
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise MutationBusy('已有治理任务正在执行，请稍后再试')
    try:
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()

def _is_sidecar_of(stem, other_stem):
    """other_stem 是否属于 stem 这条 STRM 的附属文件：同 stem，或 stem 后紧跟分隔符，
    如 A.S01E01-mediainfo / A.S01E01.zh。只看前缀会把「A - S01E1」的附属扩到「A - S01E10」上。"""
    if other_stem == stem:
        return True
    return other_stem.startswith(stem) and other_stem[len(stem)] in _SIDECAR_SEPS

def _load_wash_residuals():
    try:
        raw = json.loads(state.WASH_RESIDUAL_FILE.read_text(encoding='utf-8'))
        items = raw.get('items') if isinstance(raw, dict) else raw
        return items if isinstance(items, list) else []
    except (OSError, ValueError, TypeError):
        return []

def _save_wash_residuals(items):
    state.STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = state.WASH_RESIDUAL_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps({'version': 1, 'items': items}, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(state.WASH_RESIDUAL_FILE)

def _record_wash_residuals(records):
    if not records:
        return
    with state._WASH_RESIDUAL_LOCK:
        items = _load_wash_residuals()
        seen = {(str(x.get('strm_path')), str(x.get('sidecar_path'))) for x in items}
        for r in records:
            key = (str(r.get('strm_path')), str(r.get('sidecar_path')))
            if key not in seen:
                items.append(r)
                seen.add(key)
        # 保留最近 5000 条，避免长期运行的状态文件无限增长。
        _save_wash_residuals(items[-5000:])

def _remove_strm(f):
    """删除 STRM，并删除归属于它的 sidecar。

    返回 ``{'sidecars_removed': n, 'sidecars_failed': [...]} ``。
    关键点：sidecar 删除失败不再静默吞掉；STRM 本体删除成功后会写入
    wash_residuals.json，后续「洗版残留」只认这份删除血缘，不再把任意
    metadata-only 目录猜成残留。
    """
    stem = f.stem
    siblings = [x for x in f.parent.iterdir() if x.is_file()]
    strm_stems = [x.stem for x in siblings if x.suffix.lower() == '.strm']
    failed = []
    removed = 0
    for sibling in siblings:
        if sibling.name == f.name or sibling.suffix.lower() == '.strm':
            continue
        owners = [x for x in strm_stems if _is_sidecar_of(x, sibling.stem)]
        if owners and max(owners, key=len) == stem:
            try:
                sibling.unlink()
                removed += 1
            except OSError as e:
                failed.append((sibling, str(e)))
    # STRM 本体必须成功删除；失败则不写“已删除后的残留”记录。
    f.unlink()
    if failed:
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        lib = 'local' if _inside(f, state.L_ROOT) else ('share' if _inside(f, state.S_ROOT) else 'unknown')
        _record_wash_residuals([
            {'id': hashlib.sha1(f'{f}|{s}|{now}'.encode('utf-8')).hexdigest()[:16],
             'lib': lib, 'strm_path': str(f), 'sidecar_path': str(s),
             'sidecar_name': s.name, 'reason': 'sidecar_delete_failed',
             'error': err, 'created_at': now}
            for s, err in failed
        ])
    return {'sidecars_removed': removed, 'sidecars_failed': [str(s) for s, _ in failed]}

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
        return 'category' if parts[0] in state._CATEGORY_NAMES else 'unknown'
    name = path.name
    parent = path.parent
    parent_name = parent.name if parent != base_root else ''
    if parse_season_dir(name) is not None:
        return 'season'
    if parent_name and parse_season_dir(parent_name) is not None:
        return 'series_root'
    if parent_name in state._CATEGORY_NAMES:
        return 'movie'
    return 'unknown'

def _has_confirmed_residual_in_dir(d):
    """删除目录前检查本程序登记的 sidecar 残留。

    这是 1.6.4 的关键安全闸：sidecar 删除失败后，即使同目录的其它 STRM 已经
    删除，也绝不能让 _prune_up 用 rmtree 把“待人工确认”的残留顺手带走。
    """
    try:
        d = d.resolve()
    except OSError:
        return False
    for r in _load_wash_residuals():
        cp = Path(str(r.get('sidecar_path') or ''))
        try:
            if cp.resolve().parent == d and cp.is_file() and not Path(str(r.get('strm_path') or '')).exists():
                return True
        except OSError:
            continue
    return False

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
        if _has_confirmed_residual_in_dir(cur):
            log.info('目录跳过清理 [%s]: 存在已确认洗版残留，等待残留页处理: %s', kind, cur)
            return
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
          'sidecars_removed': 0, 'sidecar_residuals': [], 'errors': []}
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
            rr = _remove_strm(f)
            st['strm_removed'] += 1
            st['sidecars_removed'] += rr.get('sidecars_removed', 0)
            st['sidecar_residuals'].extend(rr.get('sidecars_failed', []))
            if rr.get('sidecars_failed'):
                st['errors'].append(f'{f.name}: {len(rr["sidecars_failed"])} 个附属文件删除失败，已登记洗版残留')
            parents.add(f.parent)
        except OSError as e:
            st['errors'].append(f'{f.name}: {e}')
    for p in sorted(parents, key=lambda x: len(x.parts), reverse=True):
        _prune_up(p, base_root, cloud_root)
    return st

def find_movie_strms_by_tmdb(root, tmdb_id):
    """按 tmdb 编号找电影目录下的 STRM（单片删除用）。
    - 目录名里的编号必须完全相等：以前按子串匹配，删 tmdb-123 会连带删掉 tmdb-1234；
    - 命中目录整体收集后不再往下走，嵌套目录不会重复计数；
    - 剧集目录跳过——电影和剧集的 tmdb 编号是两套命名空间，同号的剧集不能被「删电影」带走。
      判定为剧集：位于剧集分类下、含季目录、或含 SxxExx 命名的 STRM（不用宽松的集号规则，
      否则「星球大战 Ep 4」这类电影会被误判成剧集而删不掉）。"""
    want = str(tmdb_id).strip()
    if not want.isdigit() or not root.exists():
        return []
    want = int(want)
    out = []
    for dp, dns, _fns in os.walk(root):
        m = _RE_DIR_TMDB.search(os.path.basename(dp))
        if not m or int(m.group(1)) != want:
            continue
        dns[:] = []
        strms, is_series = [], lib._under_tv_category(Path(dp), root)
        for r, ds, ns in os.walk(dp):
            if any(parse_season_dir(d) is not None for d in ds):
                is_series = True
            strms.extend(Path(r) / n for n in ns if n.lower().endswith('.strm'))
        if is_series or any(RE_SXXEXX.search(f.name) for f in strms):
            log.info('按 tmdb 删电影：%s 是剧集目录，跳过', dp)
            continue
        out.extend(strms)
    return out

def _clamp_depth(v, default=5):
    try:
        v = int(v)
    except (ValueError, TypeError):
        v = default
    return max(1, min(10, v))

def scan_orphans(max_depth=5, diag=None):
    """
    扫描两库，返回未知文件（只报告不删，白皮书 §16）。
    用 os.walk + 深度剪枝，避免遍历大库下所有 STRM。
    diag（dict，可选）会被填入扫描诊断：每个库的路径是否存在、看过多少目录/文件、
    各类文件的数量、被深度限制跳过的目录数、读取错误、耗时。
    有了这些数字，「没结果」才分得清是真没有，还是没扫到。
    """
    t0 = time.time()
    orphans = []
    max_depth = _clamp_depth(max_depth)
    libs = []

    for root, lib_name in ((state.L_ROOT, 'local'), (state.S_ROOT, 'share')):
        info = {'lib': lib_name, 'root': str(root), 'exists': root.exists(),
                'dirs': 0, 'files': 0, 'strm': 0, 'video': 0, 'meta': 0,
                'ignored': 0, 'unknown': 0, 'pruned_dirs': 0, 'errors': []}
        libs.append(info)
        if not info['exists']:
            continue
        root_parts = len(root.parts)

        def _onerr(e, _info=info):
            if len(_info['errors']) < 5:
                _info['errors'].append(f'{e.filename}: {e.strerror or e}')

        try:
            for dp, dns, fns in os.walk(root, topdown=True, onerror=_onerr):
                cur_depth = len(Path(dp).parts) - root_parts
                info['dirs'] += 1
                if cur_depth >= max_depth:
                    # 本层文件照常检查，但不再往下走；记下有多少子目录没看
                    info['pruned_dirs'] += len(dns)
                    dns[:] = []
                for fn in fns:
                    info['files'] += 1
                    low = fn.lower()
                    ext = os.path.splitext(fn)[1].lower()
                    if low.startswith('.') or low in _ORPHAN_IGNORE_NAMES:
                        info['ignored'] += 1
                        continue
                    if ext == '.strm':
                        info['strm'] += 1
                        continue
                    if ext in VIDEO_EXTS:
                        info['video'] += 1
                        continue
                    if ext in _METADATA_EXTS:
                        info['meta'] += 1
                        continue
                    fp = os.path.join(dp, fn)
                    try:
                        sz = os.path.getsize(fp)
                    except OSError:
                        sz = 0
                    info['unknown'] += 1
                    orphans.append({'lib': lib_name, 'path': fp,
                                    'ext': ext or '(无扩展名)', 'size': sz})
        except OSError as e:
            log.warning('孤儿扫描失败 %s: %s', root, e)
            info['errors'].append(str(e))

    if diag is not None:
        diag.update({'depth': max_depth, 'libs': libs,
                     'elapsed': round(time.time() - t0, 2)})
    return orphans

def _confirmed_wash_residuals(max_depth=5):
    """返回有删除血缘的洗版残留。

    这里故意不再根据“metadata-only 目录”推断残留。只有本程序成功删除过
    某条 STRM、但该 STRM 的 sidecar 删除失败，并且当前 sidecar 仍然存在、
    原 STRM 已不存在时，才进入“已确认洗版残留”。
    """
    max_depth = _clamp_depth(max_depth)
    raw = _load_wash_residuals()
    if not raw:
        return []
    out, valid = [], []
    now = time.time()
    for r in raw:
        sp = Path(str(r.get('strm_path') or ''))
        cp = Path(str(r.get('sidecar_path') or ''))
        if not sp or not cp:
            continue
        base = state.L_ROOT if _inside(cp, state.L_ROOT) else (state.S_ROOT if _inside(cp, state.S_ROOT) else None)
        if base is None or not _inside(sp, base) or not _inside(cp, base):
            continue
        try:
            rel_depth = len(cp.relative_to(base).parts) - 1
        except (ValueError, OSError):
            continue
        # 历史记录如果已经超出当前扫描深度，保守不展示；记录仍保留，调大深度即可看到。
        if rel_depth > max_depth:
            valid.append(r)
            continue
        if sp.exists():
            # STRM 被重新入库，旧残留记录失效。
            continue
        if not cp.is_file():
            continue
        # sidecar 必须仍在原 STRM 所在目录；删除逻辑本身只处理同目录 sidecar。
        if cp.parent != sp.parent:
            continue
        rr = dict(r)
        rr.update({'path': str(cp.parent), 'sidecar_path': str(cp),
                   'file_count': 1, 'size': cp.stat().st_size if cp.exists() else 0,
                   'kind': 'movie'})
        # 根据目录结构重新分类；剧集 Season/Series root 优先。
        kind = _classify_dir(cp.parent, base)
        if kind not in ('movie', 'series_root', 'season'):
            continue
        rr['kind'] = kind
        rr['reason_label'] = '已确认：STRM 已删除但附属文件删除失败'
        rr['age_seconds'] = max(0, int(now - _parse_residual_ts(r.get('created_at')))) if _parse_residual_ts(r.get('created_at')) else None
        out.append(rr)
        valid.append(r)
    # 清掉已经消失/重新入库的旧记录；保留深度之外的记录。
    if len(valid) != len(raw):
        try:
            with state._WASH_RESIDUAL_LOCK:
                _save_wash_residuals(valid[-5000:])
        except OSError:
            pass
    # 同一目录只展示一次，避免一个目录多个失败 sidecar 重复出现。
    grouped = {}
    for r in out:
        key = (r['lib'], r['path'])
        g = grouped.setdefault(key, dict(r))
        g['residual_files'] = sorted(set((g.get('residual_files') or []) + [r['sidecar_path']]))
        g['file_count'] = len(g['residual_files'])
        g['size'] = sum(Path(x).stat().st_size for x in g['residual_files'] if Path(x).is_file())
    return sorted(grouped.values(), key=lambda x: (x['lib'], x['path']))

def _parse_residual_ts(s):
    try:
        return datetime.datetime.strptime(str(s), '%Y-%m-%d %H:%M:%S').timestamp()
    except (ValueError, TypeError, OSError):
        return 0

def scan_orphan_dirs(max_depth=5):
    """扫描“已确认洗版残留”。

    1.6.4 起不再把任意 metadata-only / 空目录猜成残留。残留必须来自本程序
    的删除血缘：STRM 已成功删除，但其 sidecar 删除失败并写入 journal。
    返回 [{'lib','path','kind','file_count','size','residual_files',
    'reason_label','strm_path',...}]。
    """
    return _confirmed_wash_residuals(max_depth=max_depth)

def clean_orphan_dirs(paths, dry_run=True):
    """删除已确认的洗版残留目录/文件。

    1.6.4 不允许仅凭“目录无 STRM”删除；路径必须仍存在于 residual journal，
    且 journal 中记录的 sidecar 仍存在、原 STRM 仍不存在。删除后刷新 journal。
    """
    if dry_run:
        return _clean_orphan_dirs(paths, True)
    try:
        with mutation_lock():
            return _clean_orphan_dirs(paths, False)
    except MutationBusy as e:
        return {'status': 'busy', 'message': str(e)}

def _clean_orphan_dirs(paths, dry_run):
    requested = {str(Path(p)) for p in (paths or [])}
    current = _confirmed_wash_residuals(max_depth=10)
    by_dir = defaultdict(list)
    for r in current:
        by_dir[str(Path(r['path']))].append(r)
    removed, errors = [], []
    if not requested:
        return {'status': 'success', 'dry_run': dry_run, 'removed': [], 'count': 0, 'errors': []}
    for p in sorted(requested):
        rs = by_dir.get(p)
        if not rs:
            errors.append(f'{p}: 不在已确认洗版残留清单，跳过')
            continue
        d = Path(p)
        # 真正删除前再次确认：目录内不能重新出现 STRM/视频；不能出现 journal 未登记的未知文件。
        base = state.L_ROOT if _inside(d, state.L_ROOT) else (state.S_ROOT if _inside(d, state.S_ROOT) else None)
        if base is None:
            errors.append(f'{p}: 不在媒体库内，跳过'); continue
        kind = _classify_dir(d, base)
        if kind not in ('movie', 'series_root', 'season'):
            errors.append(f'{p}: 不是媒体专属目录({kind})，跳过'); continue
        ok, reason = _dir_cleanable(d)
        if not ok:
            errors.append(f'{p}: {reason}，跳过'); continue
        # 目录里如果有未登记的 metadata，说明它并非单纯失败 sidecar；也不自动整目录删。
        registered = {str(Path(r['sidecar_path'])) for r in rs}
        actual_files = {str(f) for f in d.rglob('*') if f.is_file()}
        if actual_files - registered:
            errors.append(f'{p}: 存在未登记文件，跳过，避免误删'); continue
        if dry_run:
            removed.append(str(d)); continue
        try:
            shutil.rmtree(d, ignore_errors=False)
            removed.append(str(d))
        except OSError as e:
            errors.append(f'{p}: {e}')
    if not dry_run and removed:
        with state._WASH_RESIDUAL_LOCK:
            raw = _load_wash_residuals()
            kept = [r for r in raw if str(Path(r.get('sidecar_path') or '').parent) not in set(removed)]
            try:
                _save_wash_residuals(kept[-5000:])
            except OSError as e:
                errors.append(f'残留日志更新失败: {e}')
        governance.write_audit_log('洗版残留清理',
                        f'清理已确认洗版残留目录 {len(removed)} 个',
                        [f'删除 {len(removed)} 个已确认残留目录'] + removed[:50]
                        + (['错误: ' + e for e in errors[:3]] if errors else []))
    return {'status': 'success', 'dry_run': dry_run,
            'removed': removed, 'count': len(removed), 'errors': errors}

def action_scan_orphans(args):
    depth = _clamp_depth(getattr(args, "max_depth", 5))
    diag = {}
    items = scan_orphans(max_depth=depth, diag=diag)
    orphan_dirs = scan_orphan_dirs(max_depth=depth)
    libs = diag.get('libs', [])

    warnings = []
    missing = [l for l in libs if not l['exists']]
    if len(missing) == len(libs):
        return {'status': 'error',
                'message': '两个媒体库路径都不存在，没有扫描任何文件：'
                           + '；'.join(l['root'] for l in libs)
                           + '。请检查 docker-compose 里是否已挂载到 /media/local 和 /media/share。'}
    for l in missing:
        warnings.append(f"{'本地' if l['lib'] == 'local' else '分享'}库路径不存在，已跳过：{l['root']}")
    for l in libs:
        if l['exists'] and l['files'] == 0:
            warnings.append(f"{'本地' if l['lib'] == 'local' else '分享'}库里一个文件都没读到，"
                            '可能是挂载为空或权限不足')
        for er in l['errors']:
            warnings.append(f"读取出错：{er}")
        if l['pruned_dirs']:
            warnings.append(f"{'本地' if l['lib'] == 'local' else '分享'}库有 {l['pruned_dirs']} 个子目录"
                            f"超过深度 {depth}，未扫描（可调大深度）")

    by_lib = defaultdict(int)
    by_ext = defaultdict(int)
    for it in items:
        by_lib[it['lib']] += 1
        by_ext[it['ext']] += 1
    items.sort(key=lambda x: (x['lib'], x['path']))
    return {
        'status': 'success',
        'count': len(items),
        'by_lib': dict(by_lib),
        'by_ext': dict(by_ext),
        'items': items[:200],
        'orphan_dirs': orphan_dirs,
        'orphan_dir_count': len(orphan_dirs),
        'scan': {'depth': depth, 'elapsed': diag.get('elapsed', 0), 'libs': libs},
        'warnings': warnings,
    }

def action_clean_orphan_dirs(args):
    """删除孤儿目录（只报告不删的配套删除动作）。paths 由前端传入。"""
    paths = getattr(args, 'paths', []) or []
    dry_run = bool(getattr(args, 'dry_run', True))
    if not paths:
        return {'status': 'error', 'message': '未指定要删除的目录'}
    return clean_orphan_dirs(paths, dry_run=dry_run)

def purge_old():
    """清理过期治理计划（保留 7 天供审计）。"""
    if state.STATE_DIR.exists():
        plan_cut = time.time() - 7 * 86400
        for f in state.STATE_DIR.glob('plan_*.json'):
            try:
                if f.stat().st_mtime < plan_cut:
                    f.unlink()
            except OSError:
                pass
        try:
            storage.db_purge_plans(plan_cut)   # SQLite 索引同步清理
        except Exception:
            pass


# ═══════════ 目录级残留清理（照搬上游 TgtoDrive「Emby 洗版残留清理」） ═══════════
# 上游做法：递归扫容器目录 → 叶子目录内完全没有 .strm → 按空目录规则整体清理；
# 执行时移入残留备份目录（可恢复），不是直接删。ttd-guard 原先只认「删除血缘」，
# 而上游 TgtoDrive 自己洗版重命名产生的空目录没有 ttd-guard 血缘，永远扫不到——
# 这里补上目录级扫描，不依赖血缘。

def _has_strm_tree(d):
    """目录子树内是否存在 .strm（存在即非空目录残留）。"""
    for _dp, _dns, fns in os.walk(str(d)):
        for fn in fns:
            if fn.lower().endswith('.strm'):
                return True
    return False


def scan_empty_dirs(root_path, limit=100):
    """目录级残留扫描：找「整个子树内完全没有 .strm」的媒体目录。

    - 路径必须位于本地/分享库内（防误扫其他目录）
    - 只报目录名带 {tmdb-xxx} 标记的媒体目录（与上游「仅支持 TTD 命名规范」一致，
      分类目录/根目录永不合格，杜绝误删大类）
    - 自顶向下：命中最顶层空目录后剪枝，不再重复报其子目录
    返回 {'status','folders','hits','empty_dirs','preview':[...]}，
    preview 每条 {path, rel, abs_path, type, rule} 与上游预览结构对齐。
    """
    base = Path(str(root_path or ''))
    if _inside(base, state.L_ROOT):
        root = Path(state.L_ROOT)
    elif _inside(base, state.S_ROOT):
        root = Path(state.S_ROOT)
    else:
        return {'status': 'error', 'message': '扫描目录必须在本地/分享库根内'}
    folders = 0
    hits = []

    def _walk(d, depth):
        nonlocal folders
        if depth > 10:
            return
        folders += 1
        if _has_strm_tree(d):
            for sub in sorted(d.iterdir()):
                if sub.is_dir():
                    _walk(sub, depth + 1)
            return
        # 子树全空：只报「媒体目录」（目录名带 tmdb 标记），根/分类层不报
        if d == root or not _RE_DIR_TMDB.search(d.name):
            return
        hits.append(d)

    try:
        _walk(root, 0)
    except OSError as e:
        return {'status': 'error', 'message': f'扫描失败: {e}'}

    preview = []
    for d in hits[:max(1, int(limit or 100))]:
        try:
            rel = d.relative_to(root).as_posix()
        except ValueError:
            rel = d.name
        preview.append({'path': str(d), 'rel': rel, 'abs_path': str(d),
                        'type': '空目录', 'rule': '叶子目录内未发现 .strm，按空目录规则整体清理'})
    return {'status': 'success', 'folders': folders, 'hits': len(hits),
            'empty_dirs': len(hits), 'preview': preview,
            'preview_limit': max(1, int(limit or 100))}


def clean_empty_dirs(paths):
    """执行空目录清理：逐目录复核后「移动到备份目录」（可恢复），并通知 Emby 刷新。

    复核三道闸：仍在库内 / 目录名带 tmdb 标记 / 子树仍无 .strm（防止预览后又有新入库）。
    """
    moved, errors = [], []
    backup_root = None
    try:
        with mutation_lock():
            backup_root = state.STATE_DIR / 'residue_backup' / time.strftime('%Y%m%d-%H%M%S')
            for p in (paths or []):
                d = Path(str(p))
                base = state.L_ROOT if _inside(d, state.L_ROOT) else (
                    state.S_ROOT if _inside(d, state.S_ROOT) else None)
                if base is None:
                    errors.append(f'{p}: 不在媒体库内，跳过'); continue
                if not _RE_DIR_TMDB.search(d.name):
                    errors.append(f'{p}: 非媒体目录，跳过'); continue
                if _has_strm_tree(d):
                    errors.append(f'{p}: 目录内已出现 .strm，跳过'); continue
                rel = d.relative_to(base)
                dst = backup_root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(d), str(dst))
                moved.append({'path': str(d), 'backup': str(dst)})
            if moved:
                emby.notify_emby_deleted([m['path'] for m in moved], background=True)
    except MutationBusy as e:
        return {'status': 'busy', 'message': str(e)}
    except OSError as e:
        errors.append(str(e))
    return {'status': 'success', 'moved': moved, 'count': len(moved),
            'errors': errors, 'backup_root': str(backup_root) if moved else ''}
