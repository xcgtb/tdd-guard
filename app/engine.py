# -*- coding: utf-8 -*-
"""media_agent.py —— 飞牛 NAS 端治理引擎"""
import contextlib
import dataclasses
import html
import os, re, sys, json, threading, shutil, fcntl, time, argparse, datetime, hashlib, logging, traceback
import urllib.parse, urllib.request, urllib.error
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

try:
    from .core import (
        esc, parse_season_dir, get_ep, get_score, best_score,
        title_key, governance_title_key, analyze_season_episodes,
        parse_emby_library, quality_label, RE_SXXEXX,
    )
    from . import config as _cfg
except ImportError:
    from core import (
        esc, parse_season_dir, get_ep, get_score, best_score,
        title_key, governance_title_key, analyze_season_episodes,
        parse_emby_library, quality_label, RE_SXXEXX,
    )
    import config as _cfg


try:
    from . import logger
except ImportError:
    import logger


def _p(env, default):
    return Path(os.environ.get(env, default))

# 容器内路径固定：用户只需在 docker-compose 里把宿主机目录挂载到这三个位置。
# 环境变量覆盖仅供测试使用（tests/ 用它指向临时目录），不对用户开放、不写入文档。
L_ROOT = _p('L_ROOT', '/media/local')
S_ROOT = _p('S_ROOT', '/media/share')
CLOUD_L_ROOT = _p('CLOUD_L_ROOT', '/media/cloud')
# DATA_DIR 以 config 为准（config.py 是路径的唯一定义处），避免两处默认值漂移
DATA_DIR = _cfg.DATA_DIR
LOG_TXT = DATA_DIR / '媒体治理明细.log'
STATE_DIR = DATA_DIR / 'state'
LOCK_FILE = DATA_DIR / 'agent.lock'
REPORT_DIR = DATA_DIR / 'reports'
SUB_STATE_FILE = STATE_DIR / 'subscriptions_state.json'
EMBY_LIB_CACHE_FILE = STATE_DIR / 'emby_library_with_tmdb.json'
INGEST_CACHE_FILE = STATE_DIR / 'ingest_cache.json'
WASH_RESIDUAL_FILE = STATE_DIR / 'wash_residuals.json'
LIBRARY_SNAPSHOT_FILE = STATE_DIR / 'library_snapshot.json'

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
PLAN_TTL = 2 * 3600
PRUNE_MIN_DEPTH = int(os.environ.get('PRUNE_MIN_DEPTH', '3'))

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('media_agent')


def _ensure_tz():
    """统一时区：读 TZ 环境变量，缺省 Asia/Shanghai。
    镜像里没有 tzdata 时 TZ=Asia/Shanghai 会静默退回 UTC，这里检测到就改用 POSIX 写法 CST-8（不依赖 tzdata）。"""
    tz = (os.environ.get('TZ') or '').strip() or 'Asia/Shanghai'
    os.environ['TZ'] = tz
    try:
        time.tzset()
        if tz == 'Asia/Shanghai' and time.localtime().tm_gmtoff != 8 * 3600:
            log.warning('时区 Asia/Shanghai 未生效（镜像缺少 tzdata？），改用 CST-8')
            os.environ['TZ'] = 'CST-8'
            time.tzset()
    except (AttributeError, OSError) as e:
        log.warning('设置时区失败: %s', e)


_ensure_tz()


def tz_info():
    """当前进程使用的时区，供前端/健康检查核对（晨报、巡检都按这个时区算）"""
    lt = time.localtime()
    off = int(getattr(lt, 'tm_gmtoff', 0) or 0)
    sign = '+' if off >= 0 else '-'
    off = abs(off)
    return {'name': os.environ.get('TZ', ''), 'offset': f'UTC{sign}{off // 3600:02d}:{off % 3600 // 60:02d}',
            'now': time.strftime('%Y-%m-%d %H:%M:%S', lt)}


def _norm_emby_path(p):
    """Emby 路径统一成 / 分隔、去掉末尾 /；空值返回 ''"""
    p = str(p or '').strip().replace('\\', '/')
    return p.rstrip('/')


_LEGACY_LIB_NAMES = ('影视媒体库', '分享影视库')


class EmbyPathMap:
    """Emby 路径 → 本地/分享库 的唯一映射处（根目录来自配置 emby_local_path / emby_share_path）。
    片库总览要对 ~10 万个分集路径分类，所以这里只做字符串前缀比较，构造时把能算的都算好。"""

    def __init__(self, local_root, share_root):
        self.local = _norm_emby_path(local_root)
        self.share = _norm_emby_path(share_root)
        # (根目录, 根目录+'/', 库) —— 长的优先（嵌套时取更精确的那个），等长时本地在前，与旧逻辑一致
        roots = [(r, r + '/', lib) for r, lib in ((self.local, 'local'), (self.share, 'share')) if r]
        self._roots = sorted(roots, key=lambda x: -len(x[0]))
        # 兼容兜底（只用于展示）：路径里出现根目录的末级目录名。原来写死的两个名字保持旧版
        # '影视媒体库' in path 的子串判断，老用户行为不变；其它名字要求整段目录名相等，
        # 免得 tv 误中 tv2。两个根末级名相同时无法区分，干脆不兜底
        lb = self.local.rsplit('/', 1)[-1] if self.local else ''
        sb = self.share.rsplit('/', 1)[-1] if self.share else ''
        self._bases = [(b if b in _LEGACY_LIB_NAMES else '/' + b + '/', lib)
                       for b, lib in ((lb, 'local'), (sb, 'share')) if b] if lb != sb else []

    def lib_of(self, path, fallback=True):
        """展示/统计用：'local' | 'share' | ''"""
        if not path:
            return ''
        p = path.replace('\\', '/') if '\\' in path else path
        for root, pre, lib in self._roots:
            if p == root or p.startswith(pre):
                return lib
        if fallback and self._bases:
            padded = '/' + p + '/'
            for needle, lib in self._bases:
                if needle in padded:
                    return lib
        return ''

    def to_container(self, path):
        """删除用：严格前缀匹配（不走末级目录名兜底），转成容器内路径；对不上或含 .. 返回 None"""
        if not path:
            return None
        p = str(path).replace('\\', '/')
        for root, pre, lib in self._roots:
            if p.startswith(pre):
                parts = [x for x in p[len(pre):].split('/') if x and x != '.']
                if not parts or '..' in parts:
                    return None
                return (L_ROOT if lib == 'local' else S_ROOT).joinpath(*parts)
        return None


RUNTIME_CFG = _cfg.load_config()
EMBY_HOST = RUNTIME_CFG['emby_host']
EMBY_KEY  = RUNTIME_CFG['emby_key']


def emby_path_map(local_root=None, share_root=None):
    """按配置构造 EmbyPathMap；留空的一侧回落到默认值（设置页输入框的占位符就是默认值，
    清空后若变成「什么都匹配不上」，与界面提示不符）"""
    return EmbyPathMap((local_root or '').strip() or _cfg.DEFAULTS['emby_local_path'],
                       (share_root or '').strip() or _cfg.DEFAULTS['emby_share_path'])


EMBY_PATHS = emby_path_map(RUNTIME_CFG.get('emby_local_path'), RUNTIME_CFG.get('emby_share_path'))
TMDB_BASE = os.environ.get('TMDB_BASE', 'https://api.themoviedb.org/3')
TMDB_LANG = os.environ.get('TMDB_LANG', 'zh-CN')
TMDB_IMG  = os.environ.get('TMDB_IMG', 'https://image.tmdb.org/t/p/w500')
TMDB_INFO_TTL = 24 * 3600


def reload_config():
    global RUNTIME_CFG, EMBY_HOST, EMBY_KEY, EMBY_PATHS
    RUNTIME_CFG = _cfg.load_config()
    EMBY_HOST = RUNTIME_CFG['emby_host'] or 'http://127.0.0.1:8096'
    EMBY_KEY = RUNTIME_CFG['emby_key'] or ''
    EMBY_PATHS = emby_path_map(RUNTIME_CFG.get('emby_local_path'), RUNTIME_CFG.get('emby_share_path'))
    return RUNTIME_CFG


def emby_lib_of(path):
    """Emby 路径属于哪个库（展示/统计用，带末级目录名兜底）：'local' | 'share' | ''"""
    return EMBY_PATHS.lib_of(path)


def _strategy():
    return _cfg.get_strategy()

def _exempt_keywords():
    return _strategy()['exempt_keywords']

def _ep(p: Path):
    parent = p.parent
    allow = parse_season_dir(parent.name) is not None
    if not allow:
        # 从当前路径向上找本工具的媒体根；只有位于含“剧”的分类目录下才允许
        # 裸 EP/Episode。找不到根时宁可保守返回 None。
        for root in (L_ROOT, S_ROOT):
            try:
                parent.relative_to(root)
                allow = _under_tv_category(parent, root)
                break
            except (ValueError, OSError):
                continue
    return get_ep(p.name, parent.name, allow_bare_ep=allow)

def _best(files):
    return best_score(f.name for f in files)

def _exempt_hit(name, kws=None):
    if kws is None:
        kws = _exempt_keywords()
    if not kws:
        return []
    s = str(name)
    return [k for k in kws if k in s]


def _nat_key(text):
    """自然排序：Season 9 < Season 10"""
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', str(text))]


def _split_root(path):
    """返回 (库标签, 相对根目录的路径)；不在两个库内则 ('', None)"""
    p = Path(path)
    for root, label in ((L_ROOT, '本地'), (S_ROOT, '分享')):
        try:
            return label, p.relative_to(root)
        except ValueError:
            continue
    return '', None


def _exempt_group_of(path, kws):
    """命中白名单的目录层级名，如 综艺/百家讲坛/Season 451/x.strm -> ('百家讲坛', 关键词)。
    只看目录层，不看文件名；找不到返回 (None, None)"""
    _, rel = _split_root(path)
    if rel is None:
        return None, None
    for part in rel.parts[:-1]:
        for k in kws:
            if k in part:
                return part, k
    return None, None

def _share_wins(s_q, l_q):
    s = _strategy()
    decision = s['decision']
    if decision == 'keep_local': return False
    if decision == 'keep_share': return True
    return s_q > l_q if s['tie_keep_local'] else s_q >= l_q


# ═══════════════════ 电视剧逐集比较（白皮书 §7）═══════════════════
def _season_compare(s_files, l_files):
    """
    逐集对齐比较分享与本地同一季（取代双向保留，改为单向择优）。
    同集号多份时取各自最高画质参与比较。
    返回 dict：
      s_eps / l_eps : {集号: 最高画质文件}
      s_only / l_only : 各自独有的集号列表（含前序缺失、中间断层）
      common : 交集集号
      s_better : 交集内分享画质 >= 本地的集数
      l_better : 交集内本地画质 > 分享的集数
    """
    s_eps, l_eps = {}, {}
    for f in (s_files or []):
        ep = _ep(f)
        # ep[0]<=0: S00 目录；ep[1]<=0: E00 第0集（特别集）——均不参与对照与完整性判定
        if not ep or ep[0] <= 0 or ep[1] <= 0:
            continue
        old = s_eps.get(ep[1])
        if old is None or get_score(f.name) > get_score(old.name):
            s_eps[ep[1]] = f
    for f in (l_files or []):
        ep = _ep(f)
        if not ep or ep[0] <= 0 or ep[1] <= 0:
            continue
        old = l_eps.get(ep[1])
        if old is None or get_score(f.name) > get_score(old.name):
            l_eps[ep[1]] = f
    s_keys, l_keys = set(s_eps), set(l_eps)
    common = s_keys & l_keys
    s_better = l_better = 0
    for e in common:
        if get_score(s_eps[e].name) >= get_score(l_eps[e].name):
            s_better += 1
        else:
            l_better += 1
    # 独立判定完整性：本地、分享各自看正片（E01 起）是否连续无断层。
    # E00（第0集）是特别集，不参与；前序缺失（min>1）或中间断层均为残次品，两边各自独立判定。
    def _side_complete(keys):
        ks = {k for k in keys if k > 0}
        if not ks:
            return False
        lo, hi = min(ks), max(ks)
        return lo == 1 and len(ks) == (hi - lo + 1)
    return {
        's_eps': s_eps, 'l_eps': l_eps,
        's_only': sorted(s_keys - l_keys),
        'l_only': sorted(l_keys - s_keys),
        'common': common, 's_better': s_better, 'l_better': l_better,
        's_complete': _side_complete(s_keys),
        'l_complete': _side_complete(l_keys),
        'complete': _side_complete(s_keys) and _side_complete(l_keys),
    }


# 逐集择优阈值：交集内分享画质达标集数占比 >= 该比例才删本地（否则删分享或保留）。
SEASON_REPLACE_RATIO = 0.9


def _side_gap_desc(keys):
    """生成某侧缺失的具体描述，如：
      '中间断层 (缺 E10，疑似被和谐)' / '缺失前序 (缺 E01-E05)'；完整返回 '完整'。"""
    ok, reason = analyze_season_episodes(keys, '', ())
    return reason


# ═══════════════════ Emby ═══════════════════
EMBY_TIMEOUT = int(os.environ.get('EMBY_TIMEOUT', '30') or 30)   # 秒；大库 / NAS 繁忙时 15 秒容易超时
EMBY_RETRIES = 2                                                  # 仅 GET 在超时 / 5xx / 连接错误时重试


def emby_request(path, params=None, method='GET', timeout=None, body=None, retries=None):
    if not EMBY_KEY:
        raise RuntimeError('未配置 EMBY_KEY')
    timeout = timeout or EMBY_TIMEOUT
    url = EMBY_HOST + path + ('?' + urllib.parse.urlencode(params) if params else '')
    headers = {'X-Emby-Token': EMBY_KEY}
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
    root = L_ROOT if target == 'local' else S_ROOT
    eroot = EMBY_PATHS.local if target == 'local' else EMBY_PATHS.share
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


# ═══════════════════ Telegram 排版助手 ═══════════════════
_WEEK = '一二三四五六日'


def tg_title(icon, title, sub=''):
    """消息标题：粗体标题 + 斜体副标题（不再用 ━━ 分隔线，宽度在不同字体下对不齐）"""
    return f'{icon} <b>{title}</b>' + (f'\n<i>{sub}</i>' if sub else '')


def tg_row(icon, label, value, note=''):
    return f'{icon} {label}　<b>{value}</b>' + (f'　<i>{note}</i>' if note else '')


def tg_stamp():
    n = datetime.datetime.now()
    return n.strftime('%m-%d %H:%M')


def fmt_scan_text(icon, title, loc, shr, keep, ex_cnt, ex_groups=0, sub=''):
    note = f'合并为 {ex_groups} 组' if ex_groups and ex_groups != ex_cnt else ''
    return '\n'.join([
        tg_title(icon, title, sub or tg_stamp()), '',
        tg_row('🧹', '待清理本地', loc),
        tg_row('📤', '待淘汰分享', shr),
        tg_row('🛡️', '受保护', keep),
        tg_row('🏷️', '白名单豁免', ex_cnt, note),
    ])


def split_telegram_html(text, limit=3800):
    """按行切分超长 Telegram HTML 消息；切分点若落在可折叠引用块 <blockquote expandable>
    里，会在上一段补 </blockquote>、下一段重新打开，保证每段都是合法的折叠块。"""
    if len(text) <= limit:
        return [text]
    bq_open = '<blockquote expandable>'
    chunks, cur, curlen, in_bq = [], [], 0, False
    for line in text.split('\n'):
        add = len(line) + 1
        if cur and curlen + add + 14 > limit:
            chunk = '\n'.join(cur) + ('\n</blockquote>' if in_bq else '')
            chunks.append(chunk)
            cur = []
            curlen = 0
            if in_bq:
                line = bq_open + line  # 和第一行拼在同一行，避免引用块开头多一个空行
                add = len(line) + 1
        cur.append(line); curlen += add
        if '<blockquote' in line:
            in_bq = '</blockquote>' not in line
        elif '</blockquote>' in line:
            in_bq = False
    if cur:
        chunks.append('\n'.join(cur))
    return chunks


def notify_telegram(text, chat_id=None):
    token = (RUNTIME_CFG.get('telegram_bot_token') or '').strip()
    cid = chat_id or (RUNTIME_CFG.get('telegram_chat_id') or '').strip()
    if not token or not cid:
        return False
    ok_all = True
    for part in split_telegram_html(text):
        try:
            url = f'https://api.telegram.org/bot{token}/sendMessage'
            data = urllib.parse.urlencode({'chat_id': cid, 'text': part, 'parse_mode': 'HTML',
                                           'disable_web_page_preview': 'true'}).encode()
            req = urllib.request.Request(url, data=data, method='POST')
            with urllib.request.urlopen(req, timeout=10) as r:
                ok_all = ok_all and (200 <= r.status < 300)
        except Exception as e:
            log.warning('Telegram 推送失败: %s', e)
            ok_all = False
    return ok_all


def parse_dt(s):
    if not s: return None
    try:
        return datetime.datetime.fromisoformat(s.split('.')[0].rstrip('Z')).replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def write_audit_log(category, title, details=None):
    try:
        logger.write(category, title, details, rule_sig=_current_rule_snapshot()['sig'])
    except Exception as e:
        log.warning('审计日志写入失败: %s', e)

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


# ═══════════════════ Lib 短时效缓存 ═══════════════════
# 作用：短时间内多个只读入口（白名单命中预览等）连续调用 build_plan/_get_lib 时，
# 30 秒内复用同一份双库遍历结果，大库上省几秒到几十秒。
# 注意：「扫描双库」完成后和「清理成功」后都会主动失效缓存——
# 执行清理前的二次校验必须基于最新磁盘状态，不能吃扫描时的快照。
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
_WASH_RESIDUAL_LOCK = threading.Lock()
_ep_lock = threading.Lock()

def _fetch_all_episodes(force=False):
    """分页拉取全库 Episode（带 600 秒缓存 + 线程锁，避免并发重复拉）"""
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


class MutationBusy(Exception):
    pass


@contextlib.contextmanager
def mutation_lock():
    """跨入口互斥锁：CLI / Web / Telegram Bot 任何真正改文件的操作（双库清理、单剧删除、
    洗版残留清理）都必须先拿到这把文件锁，拿不到抛 MutationBusy，由调用方回「忙」。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_FILE, 'w')
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


_SIDECAR_SEPS = '.-_ [('


def _is_sidecar_of(stem, other_stem):
    """other_stem 是否属于 stem 这条 STRM 的附属文件：同 stem，或 stem 后紧跟分隔符，
    如 A.S01E01-mediainfo / A.S01E01.zh。只看前缀会把「A - S01E1」的附属扩到「A - S01E10」上。"""
    if other_stem == stem:
        return True
    return other_stem.startswith(stem) and other_stem[len(stem)] in _SIDECAR_SEPS


def _load_wash_residuals():
    try:
        raw = json.loads(WASH_RESIDUAL_FILE.read_text(encoding='utf-8'))
        items = raw.get('items') if isinstance(raw, dict) else raw
        return items if isinstance(items, list) else []
    except (OSError, ValueError, TypeError):
        return []


def _save_wash_residuals(items):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = WASH_RESIDUAL_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps({'version': 1, 'items': items}, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(WASH_RESIDUAL_FILE)


def _record_wash_residuals(records):
    if not records:
        return
    with _WASH_RESIDUAL_LOCK:
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
        lib = 'local' if _inside(f, L_ROOT) else ('share' if _inside(f, S_ROOT) else 'unknown')
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


def _under_tv_category(d, root):
    try:
        parts = d.relative_to(root).parts
    except ValueError:
        return False
    return any(p in _CATEGORY_NAMES and '剧' in p for p in parts)


_RE_DIR_TMDB = re.compile(r'(?i)tmdb(?:id)?[-_=: ]*(\d+)')


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
        strms, is_series = [], _under_tv_category(Path(dp), root)
        for r, ds, ns in os.walk(dp):
            if any(parse_season_dir(d) is not None for d in ds):
                is_series = True
            strms.extend(Path(r) / n for n in ns if n.lower().endswith('.strm'))
        if is_series or any(RE_SXXEXX.search(f.name) for f in strms):
            log.info('按 tmdb 删电影：%s 是剧集目录，跳过', dp)
            continue
        out.extend(strms)
    return out


_ORPHAN_IGNORE_NAMES = {'thumbs.db', 'desktop.ini', '.ds_store'}


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

    for root, lib_name in ((L_ROOT, 'local'), (S_ROOT, 'share')):
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
        base = L_ROOT if _inside(cp, L_ROOT) else (S_ROOT if _inside(cp, S_ROOT) else None)
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
            with _WASH_RESIDUAL_LOCK:
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
        base = L_ROOT if _inside(d, L_ROOT) else (S_ROOT if _inside(d, S_ROOT) else None)
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
        with _WASH_RESIDUAL_LOCK:
            raw = _load_wash_residuals()
            kept = [r for r in raw if str(Path(r.get('sidecar_path') or '').parent) not in set(removed)]
            try:
                _save_wash_residuals(kept[-5000:])
            except OSError as e:
                errors.append(f'残留日志更新失败: {e}')
        write_audit_log('洗版残留清理',
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


def _media_title_folder(path):
    """从 STRM 路径取得实际媒体标题目录。Season/S00 目录向上一级。"""
    try:
        p = Path(path)
        if parse_season_dir(p.parent.name) is not None:
            return p.parent.parent.name
        return p.parent.name
    except Exception:
        return ''


def _tmdb_ids_from_files(files):
    ids = []
    for f in files or []:
        folder = _media_title_folder(f)
        m = re.search(r'(?i)tmdb(?:id)?[=\-: ]*(\d+)', folder or '')
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    return ids


def _governance_evidence(title, year, share_files=None, local_files=None):
    """为治理项保留可审计的匹配证据。

    治理匹配的唯一业务依据是「规范化剧名 + 年份」；TMDB 只作为诊断信息。
    如果两边目录写入了不同 TMDB，仍然允许同名同年匹配；如果同一个 TMDB
    同时出现在不同标题/年份，调用方应把它视为元数据冲突，而不是同片依据。
    """
    share_files = list(share_files or [])
    local_files = list(local_files or [])
    sp = sorted({str(Path(f)) for f in share_files})
    lp = sorted({str(Path(f)) for f in local_files})
    st = sorted(set(_tmdb_ids_from_files(share_files)))
    lt = sorted(set(_tmdb_ids_from_files(local_files)))
    return {
        'match_basis': '剧名 + 年份',
        'match_basis_detail': f'规范化剧名「{title}」 + 年份「{year or "未知"}」',
        'share_paths': sp[:20],
        'local_paths': lp[:20],
        'share_tmdb': st,
        'local_tmdb': lt,
        'tmdb': sorted(set(st) | set(lt)),
        'tmdb_consistent': (not st or not lt or bool(set(st) & set(lt))),
    }


def _attach_governance_evidence(meta, title, year, share_files=None, local_files=None):
    m = dict(meta or {})
    ev = _governance_evidence(title, year, share_files, local_files)
    m.update(ev)
    return m


def _act_to_dict(a: Act) -> dict:
    return {
        'text': a.text, 'detail': a.detail,
        'reason': a.meta.get('reason', ''),
        'reason_label': a.meta.get('reason_label', ''),
        'title': a.meta.get('title', ''),
        'season': a.meta.get('season'),
        'meta': a.meta, 'files_count': len(a.files),
    }


def _group_exempt_acts(acts):
    """把白名单豁免项按命中的目录聚合。
    一个节目下有几百个 Season 目录时，只出一条「《百家讲坛》· N 项」，明细放进 meta.members。
    只有单条的分组保持原样输出。"""
    kws = _exempt_keywords()
    groups = {}
    order = []
    for a in acts:
        title = a.meta.get('title') or ''
        gname = None
        libs = set()
        for i, f in enumerate(a.files):
            lib, _ = _split_root(f)
            if lib:
                libs.add(lib)
            if gname is None:
                gname, _k = _exempt_group_of(f, kws)
            if (gname or i >= 200) and libs:
                break
        gname = gname or title or a.text
        if gname not in groups:
            groups[gname] = []
            order.append(gname)
        groups[gname].append((a, libs))
    out = []
    for gname in order:
        items = groups[gname]
        if len(items) == 1:
            out.append(_act_to_dict(items[0][0]))
            continue
        members, kwset, libset, files = [], set(), set(), 0
        for a, libs in items:
            sn = a.meta.get('season')
            t = a.meta.get('title') or ''
            if re.fullmatch(r'(?i)season\s*\d+', t.strip()):
                tag = t.strip()  # 目录名本身就是「Season 451」，S01 是文件名解析的假季号
            else:
                tag = f'《{t}》' + (f'S{int(sn):02d}' if sn is not None else '')
            if tag not in members:
                members.append(tag)
            kwset.update(a.meta.get('keywords') or [])
            libset |= libs
            files += len(a.files)
        members.sort(key=_nat_key)
        kw_txt = ','.join(sorted(kwset))
        out.append({
            'text': f'🛡️ 《{gname}》 (白名单豁免)',
            'detail': f'命中白名单 [{kw_txt}] ➔ 跳过清理 · 共 {len(members)} 项',
            'reason': 'whitelist', 'reason_label': '白名单豁免',
            'title': gname, 'season': None,
            'meta': {'reason': 'whitelist', 'reason_label': '白名单豁免',
                     'title': gname, 'keywords': sorted(kwset),
                     'libs': sorted(libset), 'member_count': len(members),
                     'members': members[:300]},
            'files_count': files,
        })
    return out


# ═══════════════════ 入库静默期 ═══════════════════
# 剧集/电影刚入库时往往只入了一部分（分享转存、STRM 还在陆续生成），
# 此时对比双库会得到"分享仅 3 集、本地 6 集 → 淘汰分享"这类偏差结论。
# 规则：一个标题（含双库、所有季）的任一目录在 ingest_quiet_minutes 分钟内有新增/变动，
# 就整体不进入治理队列（静默跳过，不出现在清单里）；过了静默期再自然纳入。
# 判定用目录 mtime（每个季目录只 stat 一次，几千次即可覆盖整库），不逐文件 stat。
_QUIET_LAST = {'n': 0}


def _ingest_quiet_minutes():
    v = os.environ.get('INGEST_QUIET_MINUTES')  # 环境变量优先（测试用，也便于临时关闭）
    if v is None:
        v = _cfg.load_config().get('ingest_quiet_minutes', '15')
    try:
        m = int(float(str(v).strip()))
    except (TypeError, ValueError):
        m = 15
    return max(0, min(m, 24 * 60))


class _QuietGate:
    def __init__(self, minutes=None):
        self.minutes = _ingest_quiet_minutes() if minutes is None else int(minutes)
        self.cutoff = time.time() - self.minutes * 60
        self._dirs = {}
        self.skipped = 0

    def _dir_recent(self, d):
        v = self._dirs.get(d)
        if v is None:
            try:
                v = os.stat(d).st_mtime >= self.cutoff
            except OSError:
                v = False
            self._dirs[d] = v
        return v

    def skip(self, *file_groups):
        """任一文件所在目录在静默期内有变动 → True（调用方应静默跳过该标题）"""
        if self.minutes <= 0:
            return False
        for files in file_groups:
            for f in files:
                if self._dir_recent(os.path.dirname(os.fspath(f))):
                    self.skipped += 1
                    return True
        return False


def _identity_conflicts(S, L):
    """找出同一个 TMDB ID 被不同「剧名+年份」目录使用的情况。

    这是元数据诊断，不参与自动删除：TMDB 可能被整理器误写，程序不能仅凭
    一个冲突 ID 猜哪一边应该删。用户可在治理详情里看到双方路径后人工处理。
    """
    by_tmdb = defaultdict(list)
    for lib_name, lib in (('local', L), ('share', S)):
        for tid_full, keys in lib.tmdb_refs.items():
            # tid_full 形如 'movie:103' / 'tv:103'：电影与剧集是独立 ID 空间，分开聚合
            kind, _, tid = tid_full.partition(':')
            for key in keys:
                disp, base, year = lib.meta.get(key, ('', '', None))
                by_tmdb[tid_full].append({'lib': lib_name, 'title': disp, 'base': base,
                                          'year': year, 'key': key, 'kind': kind, 'tmdb': tid})
    out = []
    for tid_full, rows in sorted(by_tmdb.items()):
        identities = {(r['base'].casefold(), r.get('year') or '') for r in rows}
        if len(identities) <= 1:
            continue
        # 同一身份在双库出现不算冲突；只有一个 TMDB 对应多个不同身份才报告。
        first = rows[0]
        out.append({'tmdb': first.get('tmdb', ''), 'kind': first.get('kind', 'movie'),
                    'count': len(rows), 'identities': [
            {'title': r['title'], 'year': r.get('year'), 'lib': r['lib']} for r in rows
        ]})
    return out


def _build_plan_with_libs():
    """生成治理计划，同时把本次用到的两个 Lib 一并返回，
    供调用方直接取 strm_count 等数据，避免为了计数再对双库做一遍全量 rglob。"""
    t_start = time.time()
    # 两个库互不相干，并行遍历：磁盘/网络挂载慢时能把等待时间叠在一起
    with ThreadPoolExecutor(max_workers=2) as _ex:
        _fs, _fl = _ex.submit(_get_lib, S_ROOT), _ex.submit(_get_lib, L_ROOT)
        S, L = _fs.result(), _fl.result()
    log.info('双库遍历完成：分享 %d / 本地 %d 个 STRM，耗时 %.1fs',
             S.strm_count, L.strm_count, time.time() - t_start)
    s = _strategy()
    kws = _exempt_keywords()
    gate = _QuietGate()
    acts = []

    for key, s_files in S.mov.items():
        lk = _match_governance_key(S, L, key, L.mov, s.get('match_strategy', 'title_year'), 'movie')
        if not lk: continue
        disp, l_files = S.meta[key][0], L.mov[lk]

        if gate.skip(s_files, l_files):  # 入库未满静默期：暂不治理
            continue

        hit_kws = _exempt_hit(disp, kws)
        if not hit_kws:
            for files in (s_files, l_files):
                for f in files:
                    hit_kws = _exempt_hit(str(f), kws)
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
        lk = _match_governance_key(S, L, key, L.tv, s.get('match_strategy', 'title_year'), 'tv')
        l_seasons = L.tv[lk] if lk else {}

        # 入库未满静默期：只要任意一季（任一侧）刚有变动，整部剧暂不治理，
        # 避免"还没入完"的剧被当成缺集/落后处理
        if gate.skip(*s_seasons.values(), *l_seasons.values()):
            continue

        hit_kws = _exempt_hit(disp, kws)
        if not hit_kws:
            for files_map in (s_seasons, l_seasons):
                for files in files_map.values():
                    for f in files:
                        hit_kws = _exempt_hit(str(f), kws)
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
            # 画质对比档（一边没有时不做处理）
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

        # ── 多季保护三档：off / compare / full ──
        multi_protect = s['multi_season_protect']
        n_local = len(l_proper)
        is_multi_local = n_local >= 2

        # full 档：本地多季合集一律保护，不删本地
        if multi_protect == 'full' and is_multi_local:
            # 只处理分享独有季吗？不——full 档下本地各季全部保护，
            # 分享库中与本地对齐的季也不动，整体跳过本剧所有正片季。
            continue

        # compare 档（即“开启”）：本地多季时，仅当分享对本地全部季逐集达标才整体删本地；
        # 否则逐季独立择优（保护本地不被部分季掏空）。
        if multi_protect == 'compare' and is_multi_local:
            _emit_season_acts_full(acts, disp, s_proper, l_proper, n_local)
            continue

        # off 档：逐季独立择优
        for sn, s_files in sorted(s_proper.items()):
            tag = f'《{disp}》S{sn:02d}'
            # 分享独有 / 本地独有：静默，不生成 action（主力是分享，列表不宜过多）
            if sn not in l_proper:
                continue
            l_files = l_proper[sn]
            _emit_season_act(acts, disp, sn, s_files, l_files)

    # ── 库内多版本去重：同片/同集存在多份 strm 时，只留画质最优的一份 ──
    _dedupe_lib_versions(acts, L, S, gate, kws)
    _QUIET_LAST['n'] = gate.skipped
    # 为每一条治理动作补齐「为什么匹配到」的证据，供 Web 详情/审计使用。
    # 这里不改变决策结果，只增加可追溯信息。
    for a in acts:
        if a.kind not in ('loc', 'shr', 'keep', 'exempt'):
            continue
        title = a.meta.get('title') or ''
        year = a.meta.get('year')
        sf, lf = [], []
        # 当前动作的文件只代表待处理一侧；根据治理标题重新从两库取同身份目录。
        # 这一步仅查内存 Lib，不会再次遍历磁盘。
        for key, mm in S.meta.items():
            if mm[0] == title or mm[1] == re.sub(r'\s*\(\d{4}\)$', '', title).strip():
                if key in S.mov:
                    sf.extend(S.mov.get(key, []))
                if key in S.tv:
                    sf.extend(f for sm in S.tv[key].values() for f in sm)
        for key, mm in L.meta.items():
            if mm[0] == title or mm[1] == re.sub(r'\s*\(\d{4}\)$', '', title).strip():
                if key in L.mov:
                    lf.extend(L.mov.get(key, []))
                if key in L.tv:
                    lf.extend(f for sm in L.tv[key].values() for f in sm)
        if not year:
            # meta 中没有年份时，从任一实际标题目录补充。
            for f in (sf + lf + list(a.files)):
                folder = _media_title_folder(f)
                y = re.search(r'(?:^|[^0-9])((?:19|20)\d{2})(?:[^0-9]|$)', folder or '')
                if y:
                    year = y.group(1); break
        a.meta = _attach_governance_evidence(a.meta, title, year, sf or ([f for f in a.files] if a.kind == 'shr' else []), lf or ([f for f in a.files] if a.kind == 'loc' else []))
        if s.get('match_strategy', 'title_year') == 'tmdb_first':
            a.meta['identity_source'] = 'tmdb_first'
            a.meta['tmdb_role'] = '优先匹配键；同 TMDB 多身份列为冲突不处理'
            a.meta['match_basis'] = 'TMDB 优先（冲突/缺失回退剧名+年份）'
        else:
            a.meta['identity_source'] = 'filesystem:title+year'
            a.meta['tmdb_role'] = '辅助元数据，不参与跨标题匹配'

    if gate.skipped:
        log.info('入库未满 %d 分钟，静默跳过 %d 个标题（不进入本次治理队列）', gate.minutes, gate.skipped)
    log.info('治理计划生成完成：%d 项，总耗时 %.1fs', len(acts), time.time() - t_start)
    return acts, S, L


def build_plan():
    return _build_plan_with_libs()[0]


def _dedupe_lib_versions(acts, L, S, gate=None, kws=None):
    """库内多版本去重（洗版/追更残留），只在本库内部对比，不跨库拆分：
      - 电影：同一 key（同 tmdb）下多份 strm → 留最优一份；
      - 剧集：同一季内同一集号多份 strm → 每集各留最优一份。
    已被库间治理覆盖（整组删除）的标题跳过；白名单跳过；S00 不参与。
    本地低版本走 CD2 联动删源（腾空间），分享低版本直接删 strm。
    不同 tmdb（如正片 vs 导演版）是不同 key，天然互不影响。"""
    if kws is None:
        kws = _exempt_keywords()
    if gate is None:
        gate = _QuietGate()
    covered = {(a.kind, a.media_key) for a in acts if a.kind in ('loc', 'shr')}
    for lib, kind in ((L, 'loc'), (S, 'shr')):
        # 电影
        for key, files in lib.mov.items():
            if len(files) < 2:
                continue
            disp = lib.meta[key][0]
            if gate.skip(files):  # 入库未满静默期
                continue
            # 白名单：标题或任一文件路径命中都豁免（Season NNN 类目录标题不含关键字，必须查路径）
            if _exempt_hit(disp, kws) or any(_exempt_hit(str(f), kws) for f in files):
                continue
            if (kind, f'movie:{disp}') in covered:
                continue
            best = max(files, key=lambda f: get_score(f.name))
            losers = [f for f in files if f is not best]
            cloud_note = 'CD2联动删除115网盘低版本源' if kind == 'loc' else '清理分享影视库低版本strm'
            acts.append(Act(kind, f'🎬 《{disp}》 (同片多版本 {len(files)} 份 → 留最优删其余)',
                            f'├─ 🎬 《{disp}》: 库内多版本去重（保留 {quality_label(best.name)}） ➔ {cloud_note}',
                            losers,
                            meta={'reason': 'dup_version_local' if kind == 'loc' else 'dup_version_share',
                                  'reason_label': '库内多版本-删低版',
                                  'title': disp,
                                  'keep_label': quality_label(best.name),
                                  'dup_count': len(files)}))
        # 剧集（S00 不参与，特别篇有独立策略）
        for key, seasons in lib.tv.items():
            disp = lib.meta[key][0]
            all_files = [f for fl in seasons.values() for f in fl]
            if gate.skip(all_files):  # 入库未满静默期
                continue
            if _exempt_hit(disp, kws) or any(_exempt_hit(str(f), kws) for f in all_files):
                continue
            for sn, files in seasons.items():
                if sn <= 0 or len(files) < 2:
                    continue
                by_ep = defaultdict(list)
                for f in files:
                    ep = _ep(f)
                    if not ep or ep[0] <= 0:
                        continue
                    by_ep[ep[1]].append(f)
                dup_eps = {e: fs for e, fs in by_ep.items() if len(fs) > 1}
                if not dup_eps:
                    continue
                mk = f'tv:{disp}:S{int(sn):02d}'
                if (kind, mk) in covered:
                    continue
                losers = []
                kept_labels = []
                for e, fs in sorted(dup_eps.items()):
                    best = max(fs, key=lambda f: get_score(f.name))
                    losers.extend(f for f in fs if f is not best)
                    kept_labels.append(f'E{e:02d}留{quality_label(best.name)}')
                cloud_note = 'CD2联动删除115网盘低版本源' if kind == 'loc' else '清理分享影视库低版本strm'
                tag = f'《{disp}》S{sn:02d}'
                acts.append(Act(kind, f'📺 {tag} (同集多版本 {len(dup_eps)} 集 → 各留最优)',
                                f'├─ 📺 {tag}: 库内多版本去重（{"；".join(kept_labels[:5])}） ➔ {cloud_note}',
                                losers,
                                meta={'reason': 'dup_version_local' if kind == 'loc' else 'dup_version_share',
                                      'reason_label': '库内多版本-删低版',
                                      'title': disp, 'season': sn,
                                      'dup_eps': len(dup_eps),
                                      'dup_count': len(files)}))


def _emit_season_act(acts, disp, sn, s_files, l_files):
    """单季治理：先判完整性（残次品），完整才逐集择优。

    决策规则：
      - 合并后集号不完整（前序缺 1 或中间断层）→ 视为残次品，本地+分享都删。
      - 完整：交集内逐集比画质，达标率高者保留，另一方淘汰。
    """
    tag = f'《{disp}》S{sn:02d}'
    cmp = _season_compare(s_files, l_files)
    s_total = len(cmp['s_eps'])
    l_total = len(cmp['l_eps'])
    common = len(cmp['common'])
    s_better = cmp['s_better']
    l_better = cmp['l_better']

    # ── 残次品：本地、分享各自独立判定，谁不完整删谁，并写明具体缺什么 ──
    if not cmp['l_complete'] and l_files:
        gap = _side_gap_desc(cmp['l_eps'].keys())
        acts.append(Act('loc', f'⚠️ {tag} (残次品：本地{gap} → 删本地)',
                        f'├─ ⚠️ {tag}: 本地{gap} ➔ CD2联动删除115网盘旧源',
                        l_files,
                        meta={'reason': 'incomplete_delete_local',
                              'reason_label': '残次品-删本地',
                              'title': disp, 'season': sn}))
    if not cmp['s_complete'] and s_files:
        gap = _side_gap_desc(cmp['s_eps'].keys())
        acts.append(Act('shr', f'⚠️ {tag} (残次品：分享{gap} → 删分享)',
                        f'├─ ⚠️ {tag}: 分享{gap} ➔ 清理分享影视库strm',
                        s_files,
                        meta={'reason': 'incomplete_delete_share',
                              'reason_label': '残次品-删分享',
                              'title': disp, 'season': sn}))
    if not cmp['l_complete'] or not cmp['s_complete']:
        return

    if common == 0:
        # 无交集集号，无法逐集比画质 → 静默，不删任何一边
        return

    # 分享在交集内的达标率
    share_ratio = s_better / common if common else 0.0

    # 分享集数领先（含独有集）且达标率够高 → 删本地，留分享
    if share_ratio >= SEASON_REPLACE_RATIO and s_total >= l_total:
        acts.append(Act('loc', f'📺 {tag} (分享画质达标 {s_better}/{common} 集 → 删本地腾网盘)',
                        f'├─ 📺 {tag}: 分享达标率 {share_ratio:.0%} ({s_better}/{common}集) ➔ CD2联动删除115网盘旧源',
                        l_files,
                        meta={'reason': 'share_wins',
                              'reason_label': '分享择优-删本地',
                              'title': disp, 'season': sn,
                              'share_better': s_better, 'local_better': l_better,
                              'share_total': s_total, 'local_total': l_total}))
    else:
        if s_total < l_total:
            # 分享在共同集上画质虽好，但集数明显少于本地 → 按集数优先规则保留本地
            acts.append(Act('shr', f'📺 {tag} (分享仅 {s_total} 集、本地 {l_total} 集 → 淘汰分享保留本地)',
                            f'├─ 📺 {tag}: 共同 {common} 集中分享优 {s_better} 集，但本地集数更全 ➔ 清理分享影视库strm',
                            s_files,
                            meta={'reason': 'local_wins',
                                  'reason_label': '本地更全-删分享',
                                  'title': disp, 'season': sn,
                                  'share_better': s_better, 'local_better': l_better,
                                  'share_total': s_total, 'local_total': l_total}))
        else:
            acts.append(Act('shr', f'📺 {tag} (本地更优 {l_better}/{common} 集 → 淘汰分享)',
                            f'├─ 📺 {tag}: 本地达标率 {(l_better/common):.0%} ({l_better}/{common}集) ➔ 清理分享影视库strm',
                            s_files,
                            meta={'reason': 'local_wins',
                                  'reason_label': '本地择优-删分享',
                                  'title': disp, 'season': sn,
                                  'share_better': s_better, 'local_better': l_better,
                                  'share_total': s_total, 'local_total': l_total}))


def _emit_season_acts_full(acts, disp, s_proper, l_proper, n_local):
    """多季保护 compare 档（开启）：两阶段治理。

    第一阶段：残次品治理（先剔除不完整）——本地/分享各自的残次品季（前序缺失/中间断层）
    已在此前的单季治理中处理，此处仅随多季保护结果一并呈现原因。

    第二阶段：多季保护（在剩余「完整季」上做门槛判定）——
      分享覆盖了本地全部剩余季？
      ├─ 否（未全覆盖）→ 分享这些完整季全部淘汰，不比画质（达不到替换门槛）；
      └─ 是（全覆盖且全部达标）→ 整体删本地（整剧零和，腾网盘）。
    进入第二阶段的东西已无缺集问题，故不存在「全覆盖后仍缺集」的情况。
    """
    all_pass = True
    for sn, l_files in l_proper.items():
        if sn not in s_proper:
            all_pass = False
            break
        cmp = _season_compare(s_proper[sn], l_files)
        common = len(cmp['common'])
        if common == 0:
            all_pass = False
            break
        # 单季残次品（本地或分享任一不完整）不参与「整剧零和整体删本地」
        if not cmp['l_complete'] or not cmp['s_complete']:
            all_pass = False
            break
        if cmp['s_better'] / common < SEASON_REPLACE_RATIO:
            all_pass = False
            break
        if len(cmp['s_eps']) < len(cmp['l_eps']):
            all_pass = False
            break

    if all_pass:
        for sn, l_files in sorted(l_proper.items()):
            tag = f'《{disp}》S{sn:02d}'
            acts.append(Act('loc', f'📺 {tag} (整剧零和：分享全季达标 → 删本地)',
                            f'├─ 📺 {tag}: 整剧零和 ➔ CD2联动删除115网盘旧源', l_files,
                            meta={'reason': 'multi_full_share',
                                  'reason_label': '整剧零和-分享替代',
                                  'title': disp, 'season': sn,
                                  'local_seasons': n_local,
                                  'share_seasons': len(s_proper)}))
        return

    # 未全覆盖（或存在单季问题）→ 分享这些季全部淘汰，不比画质。
    # 注：残次品在第一阶段已独立治理（本地不完整删本地），此处随之呈现原因。
    for sn, s_files in sorted(s_proper.items()):
        if sn not in l_proper:
            continue  # 分享独有季（本地没有）→ 静默，不在多季保护对比范围
        cmp = _season_compare(s_files, l_proper[sn])
        common = len(cmp['common'])
        tag = f'《{disp}》S{sn:02d}'
        # 本地残次品 → 删本地（残次品第一阶段治理，与多季保护无关）
        if not cmp['l_complete'] and l_proper[sn]:
            gap = _side_gap_desc(cmp['l_eps'].keys())
            acts.append(Act('loc', f'⚠️ {tag} (残次品：本地{gap} → 删本地)',
                            f'├─ ⚠️ {tag}: 本地{gap} ➔ CD2联动删除115网盘旧源',
                            l_proper[sn],
                            meta={'reason': 'incomplete_delete_local',
                                  'reason_label': '残次品-删本地',
                                  'title': disp, 'season': sn}))
        # 收集该季的所有治理原因（原因全列：残次品 / 集数不足 / 画质次级）
        reasons = []
        if not cmp['l_complete']:
            reasons.append('本地' + _side_gap_desc(cmp['l_eps'].keys()))
        if not cmp['s_complete']:
            reasons.append('分享' + _side_gap_desc(cmp['s_eps'].keys()))
        if cmp['l_complete'] and cmp['s_complete']:
            if len(cmp['s_eps']) < len(cmp['l_eps']):
                reasons.append(f'分享副本集数不足 ({len(cmp["s_eps"])}/{len(cmp["l_eps"])}集)')
            elif cmp['l_better'] > cmp['s_better']:
                reasons.append(f'分享画质次级 ({cmp["l_better"]}/{common}集)')
        # 未全覆盖 → 淘汰分享（本地不动）
        detail_parts = ['多季保护：分享未全覆盖本地（不替换）']
        if reasons:
            detail_parts.append('；'.join(reasons))
        acts.append(Act('shr', f'📺 {tag} (多季保护·分享未全覆盖 → 淘汰分享)',
                        f'├─ 📺 {tag}: {"；".join(detail_parts)} ➔ 清理分享影视库strm',
                        s_files,
                        meta={'reason': 'multi_protect_partial_share',
                              'reason_label': '多季保护-淘汰分享',
                              'title': disp, 'season': sn,
                              'reasons': reasons,
                              'local_seasons': n_local,
                              'share_seasons': len(s_proper)}))


def _current_rule_snapshot():
    """返回当前治理相关规则及稳定指纹。所有扫描/计划/执行记录共用这一口径。"""
    cfg = _cfg.load_config()
    strategy = _cfg.get_strategy()
    rule_keys = ('strategy_decision','strategy_multi_season_protect','strategy_tie_keep_local',
                 'strategy_exempt_keywords','strategy_special_action','ingest_quiet_minutes',
                 'ingest_interval_min','ingest_enabled')
    rules = {k: cfg.get(k, '') for k in rule_keys}
    rules['strategy'] = strategy
    raw = json.dumps(rules, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    sig = hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]
    return {'sig': sig, 'rules': rules}


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
            'meta': a.meta,
            'files': [str(f) for f in a.files],
        })
    stats = {
        'loc': sum(1 for a in acts if a.kind == 'loc'),
        'shr': sum(1 for a in acts if a.kind == 'shr'),
        'keep': sum(1 for a in acts if a.kind == 'keep'),
        'exempt': sum(1 for a in acts if a.kind == 'exempt'),
    }
    rule_snapshot = _current_rule_snapshot()
    payload = {
        'schema_version': 2,
        'id': pid,
        'ts': ts,
        'rule_sig': rule_snapshot['sig'],
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


GOV_LATEST_FILE = STATE_DIR / 'gov_latest.json'


def save_latest_scan(plan_id, result):
    """保存最近一次治理扫描的统一快照。

    ``gov_latest.json`` 是治理总览的事实来源；即使本次 0 待处理也必须更新，
    这样“上次扫描”不会错误地停留在旧 plan。
    """
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.time()
        meta = dict(result or {})
        meta['scan_ts'] = ts
        meta['scan_id'] = plan_id
        rule_sig = _current_rule_snapshot()['sig']
        meta['rule_sig'] = rule_sig
        tmp = GOV_LATEST_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps({'schema_version': 2, 'ts': ts, 'plan_id': plan_id,
                                   'rule_sig': rule_sig, 'result': meta}, ensure_ascii=False), encoding='utf-8')
        tmp.replace(GOV_LATEST_FILE)
    except OSError as ex:
        log.warning('保存最近扫描结果失败: %s', ex)


def load_latest_scan():
    try:
        return json.loads(GOV_LATEST_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def action_inter_check(args):
    acts, S_lib, L_lib = _build_plan_with_libs()
    identity_conflicts = _identity_conflicts(S_lib, L_lib)

    # 扫描完成后顺便刷新 STRM 计数缓存。
    # 计数直接取自 build_plan 刚遍历出来的 Lib（同一次遍历，数据与计划严格一致），
    # 不再为了计数把双库重新 rglob 一遍——大库/网络挂载上这是扫描耗时翻倍的元凶。
    try:
        _strm_count_cache.update({'ts': time.time(), 'local': L_lib.strm_count, 'share': S_lib.strm_count})
        _save_strm_count_disk(L_lib.strm_count, S_lib.strm_count)
        log.info('扫描后刷新 STRM 计数: local=%d share=%d', L_lib.strm_count, S_lib.strm_count)
    except Exception as e:
        log.warning('刷新 STRM 计数缓存失败: %s', e)
    # 扫描后失效 Lib 缓存：后续「执行清理」会重新 build_plan 二次校验，
    # 必须基于最新磁盘状态，不能复用扫描时的快照（清理是删文件操作，宁可多扫一次）。
    _invalidate_lib_cache()
    loc = [a for a in acts if a.kind == 'loc']
    shr = [a for a in acts if a.kind == 'shr']
    keep = [a for a in acts if a.kind == 'keep']
    exempted = [a for a in acts if a.kind == 'exempt']
    pid = save_plan(acts)
    write_audit_log('库间查重巡检',
                    f'扫描完成：待清理本地 {len(loc)} 项, 待清理分享 {len(shr)} 项, 保护 {len(keep)} 项, 豁免 {len(exempted)} 项')
    if (loc or shr) and not getattr(args, 'silent', False):
        notify_telegram(fmt_scan_text('🔍', '双库扫描完成', len(loc), len(shr), len(keep), len(exempted),
                                      len(_group_exempt_acts(exempted))))
    def _file_count(items):
        return sum(len(a.files) for a in items)
    reason_counts = defaultdict(int)
    for a in acts:
        reason = a.meta.get('reason') or ('protected' if a.kind == 'keep' else 'exempt' if a.kind == 'exempt' else 'unknown')
        reason_counts[reason] += 1
    result = {
        'status': 'success', 'plan_id': pid,
        'total_clean_cnt': len(loc) + len(shr),
        'del_local_cnt': len(loc), 'del_share_cnt': len(shr),
        'del_local_files': _file_count(loc), 'del_share_files': _file_count(shr),
        'protected_files': _file_count(keep), 'exempted_files': _file_count(exempted),
        'del_local_items': [_act_to_dict(a) for a in loc],
        'del_share_items': [_act_to_dict(a) for a in shr],
        'protected_items': [_act_to_dict(a) for a in keep],
        'exempted_items':  _group_exempt_acts(exempted),
        'exempted_count':  len(exempted),
        'quiet_skipped':   _QUIET_LAST['n'],
        'reason_counts': dict(reason_counts),
        'strm_counts': {'local': L_lib.strm_count, 'share': S_lib.strm_count,
                        'total': L_lib.strm_count + S_lib.strm_count},
        'identity_conflicts': identity_conflicts,
        'identity_conflict_count': len(identity_conflicts),
    }
    save_latest_scan(pid, result)
    return result


def action_inter_clean(args):
    # 统一互斥锁：不管从 CLI、Web 任务队列还是 Telegram Bot 线程发起，
    # 只要是真正会修改文件的清理（非 dry-run），都必须先拿到这把跨入口的文件锁，
    # 避免三个入口各自维护自己的锁导致两个清理任务同时跑。
    if args.dry_run:
        return _action_inter_clean_locked(args)
    try:
        with mutation_lock():
            return _action_inter_clean_locked(args)
    except MutationBusy as e:
        return {'status': 'busy', 'message': str(e)}


def _action_inter_clean_locked(args):
    try:
        return _run_inter_clean(args)
    except Exception as e:
        # 执行中途异常：计划标记为 failed，不能停在 executing（网页会一直显示"执行中"）
        if args.plan and not args.dry_run:
            save_plan_state(args.plan, 'failed', {'executed_at': time.time(),
                                                  'executed_result': {'error': f'{type(e).__name__}: {e}'}})
        raise


def _confirmed_files(cur, old_act):
    """二次校验的文件级收敛：只删「用户确认过的计划」和「当前重扫」都要删的文件。
    以前直接用重扫结果的文件列表，扫描后新出现的文件（例如洗版进来一个更高版本，
    原来的最优版变成了低版本）会在用户没看到的情况下被一起删掉。"""
    planned = set(old_act.get('files') or [])
    keep = [f for f in cur.files if str(f) in planned]
    return keep, len(cur.files) - len(keep)


def _run_inter_clean(args):
    skipped_details = []
    unconfirmed = 0
    if args.plan:
        plan = load_plan(args.plan)
        if plan is None:
            return {'status': 'error', 'code': 'plan_not_found',
                    'message': '清理计划不存在/已损坏（可能是旧格式），请重新诊断'}
        st = plan.get('state')
        current_rule_sig = _current_rule_snapshot()['sig']
        plan_rule_sig = str(plan.get('rule_sig') or '')
        if plan_rule_sig and plan_rule_sig != current_rule_sig:
            if not args.dry_run:
                save_plan_state(args.plan, 'stale', {
                    'stale_at': time.time(),
                    'stale_reason': f'规则指纹变化: {plan_rule_sig} → {current_rule_sig}',
                })
            return {'status': 'error', 'code': 'plan_rule_changed',
                    'message': '清理计划生成后规则已变化，请重新扫描生成新计划'}
        if st in ('done', 'failed', 'stale'):
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

        _invalidate_lib_cache()  # 二次校验必须基于最新磁盘状态，不能复用 30 秒内的 Lib 快照
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
            files, extra = _confirmed_files(cur, old_act)
            if not files:
                skipped_details.append(
                    '%s → 待删文件与计划不一致' % old_act.get('text', '?'))
                continue
            unconfirmed += extra
            todo.append(dataclasses.replace(cur, files=files))
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
    if unconfirmed:
        detail.append(f'├─ ⏭ 有 {unconfirmed} 个文件是扫描后才出现的，不在已确认的计划里，本次未删除')
    if not args.dry_run:
        purge_old()
        write_audit_log('执行跨库清理',
                        f'释放本地 {n_loc} 项, 淘汰分享 {n_sh} 项 (Emby刷新: {refreshed})',
                        detail + [f'⚠️ {w}' for w in warns])
        # Bot 端发起的清理会传 notify=False：由 Bot 自己编辑确认消息，避免重复弹两条
        if (n_loc or n_sh) and getattr(args, 'notify', True):
            notify_telegram('\n'.join([
                tg_title('🗑️', '清理完成', tg_stamp()), '',
                tg_row('💾', '释放本地', n_loc),
                tg_row('📤', '淘汰分享', n_sh),
                tg_row('🔄', 'Emby 刷新', '✅' if refreshed else '❌')]))
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
            'skipped': skipped, 'unconfirmed_files': unconfirmed}


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
_EMBY_INDEX_CACHE_FILE = STATE_DIR / 'emby_index_cache.json'
_emby_index_refresh_lock = threading.Lock()


def _load_emby_index_disk():
    try:
        raw = json.loads(_EMBY_INDEX_CACHE_FILE.read_text(encoding='utf-8'))
        data = raw.get('data')
        if isinstance(data, dict): return {'ts': float(raw.get('ts', 0)), 'data': data}
    except (OSError, ValueError):
        pass
    return None


def _save_emby_index_disk(data):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _EMBY_INDEX_CACHE_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps({'ts': time.time(), 'data': data}, ensure_ascii=False), encoding='utf-8')
        tmp.replace(_EMBY_INDEX_CACHE_FILE)
    except (OSError, TypeError) as e:
        log.warning('Emby 探索索引缓存写入失败: %s', e)


def _build_emby_library_index():
    out = {}
    for item_type in ('Movie', 'Series'):
        media_key = 'tv' if item_type == 'Series' else 'movie'
        try:
            data = emby_request('/Items', {
                'Recursive': 'true', 'IncludeItemTypes': item_type,
                'Fields': 'ProviderIds,Path,ProductionYear,CommunityRating,ImageTags',
                'Limit': 50000,
            }) or {}
            for it in data.get('Items', []):
                tmdb_id = str((it.get('ProviderIds') or {}).get('Tmdb') or '')
                if not tmdb_id: continue
                key = f'{media_key}:{tmdb_id}'
                path = it.get('Path', '') or ''
                lib = emby_lib_of(path)
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
    if not _emby_index_refresh_lock.acquire(blocking=False): return
    try:
        out = _build_emby_library_index()
        _emby_index_cache.update({'ts': time.time(), 'data': out})
        _save_emby_index_disk(out)
    except Exception as e:
        log.warning('Emby 探索索引后台刷新失败: %s', e)
    finally:
        _emby_index_refresh_lock.release()


def emby_library_index(force=False):
    """探索页身份索引：内存 5 分钟 → 磁盘立即返回+后台刷新 → 首次同步构建。"""
    if not force:
        if _emby_index_cache['data'] and time.time() - _emby_index_cache['ts'] < 300:
            return _emby_index_cache['data']
        disk = _load_emby_index_disk()
        if disk:
            _emby_index_cache.update(disk)
            if time.time() - disk['ts'] >= 300:
                threading.Thread(target=_emby_index_bg_refresh, daemon=True,
                                 name='emby-index-refresh').start()
            return _emby_index_cache['data']
    out = _build_emby_library_index()
    _emby_index_cache.update({'ts': time.time(), 'data': out})
    _save_emby_index_disk(out)
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
    # 探索页只读统一 TMDB 对照缓存，不为每张海报重新请求 /tv/{id}。
    eps_map = {}; tmdb_map = {}
    if media == 'tv':
        try:
            snap = load_library_snapshot(max_age=1800) or build_library_health_snapshot()
            for sr in snap.get('series', []):
                tid = str(sr.get('tmdb_id') or '')
                if not tid: continue
                eps_map[tid] = {'local_eps': int(sr.get('local_eps', 0) or 0),
                                 'share_eps': int(sr.get('share_eps', 0) or 0),
                                 'have_eps': int(sr.get('have_eps', 0) or 0)}
                tmdb_map[tid] = dict(sr.get('tmdb_info') or {})
        except Exception as e:
            log.warning('探索页统一快照读取失败: %s', e)

    cards = []
    for item in (res.get('results') or [])[:40]:
        tmdb_id = str(item.get('id', ''))
        title = item.get('title') or item.get('name') or ''
        date_str = item.get('release_date') or item.get('first_air_date') or ''
        year_str = date_str[:4] if date_str else ''
        rating = item.get('vote_average') or 0
        poster = item.get('poster_path')
        emby_hit = emby_index.get(('tv' if media == 'tv' else 'movie') + ':' + tmdb_id)
        in_emby = emby_hit is not None
        in_local = bool(emby_hit and emby_hit.get('in_local'))
        in_share = bool(emby_hit and emby_hit.get('in_share'))
        if poster: poster_url = f'{TMDB_IMG}{poster}'
        elif in_emby and emby_hit.get('has_image'): poster_url = f'/api/emby/poster/{emby_hit["id"]}'
        else: poster_url = ''

        # 入库进度（仅剧集）：分库集数 + TMDB 总集数
        eps = None
        if media == 'tv' and in_emby:
            e = eps_map.get(tmdb_id) or {'local_eps': 0, 'share_eps': 0, 'have_eps': 0}
            tmdb_info_cached = tmdb_map.get(tmdb_id) or {}
            tmdb_total = int(tmdb_info_cached.get('tmdb_total') or 0)
            eps = {
                'local': e['local_eps'], 'share': e['share_eps'],
                'have': e['have_eps'], 'total': tmdb_total,
            }

        cards.append({
            'tmdb_id': tmdb_id, 'title': title, 'year': year_str,
            'rating': round(rating, 1) if rating else None, 'poster': poster_url,
            'in_emby': in_emby, 'in_local': in_local, 'in_share': in_share,
            'emby_id': emby_hit['id'] if emby_hit else None,
            'type': 'tv' if media == 'tv' else 'movie',
            'eps': eps,
        })

    t.save()
    return {'status': 'success', 'page': page,
            'total_pages': min(res.get('total_pages', 1), 20),
            'total_results': res.get('total_results', 0),
            'cards': cards, 'is_search': bool(query),
            'tmdb_calls': t.calls, 'tmdb_hits': t.hits}


_emby_lib_cache = {'ts': 0, 'data': None}


_EMBY_OVERVIEW_CACHE_FILE = STATE_DIR / 'emby_overview_cache.json'
_overview_refresh_lock = threading.Lock()


def _save_overview_disk(out):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _EMBY_OVERVIEW_CACHE_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps({'ts': time.time(), 'data': out}, ensure_ascii=False),
                       encoding='utf-8')
        tmp.replace(_EMBY_OVERVIEW_CACHE_FILE)
    except (OSError, TypeError) as e:
        log.warning('片库映射缓存写入失败: %s', e)


def _load_overview_disk():
    """返回 {'ts': float, 'data': {...}} 或 None"""
    try:
        raw = json.loads(_EMBY_OVERVIEW_CACHE_FILE.read_text(encoding='utf-8'))
        data = raw.get('data')
        if isinstance(data, dict) and 'series' in data:
            return {'ts': float(raw.get('ts', 0)), 'data': data}
    except (OSError, ValueError):
        pass
    return None


def _overview_bg_refresh():
    """后台重建片库映射数据（单飞），完成后更新内存+磁盘缓存"""
    if not _overview_refresh_lock.acquire(blocking=False):
        return
    try:
        out = _build_emby_library_overview()
        _emby_lib_cache['ts'] = time.time()
        _emby_lib_cache['data'] = out
        _save_overview_disk(out)
        log.info('片库映射缓存后台刷新完成：剧集 %d / 电影 %d',
                 len(out.get('series', [])), len(out.get('movies', [])))
    except Exception as e:
        log.warning('片库映射后台刷新失败: %s', e)
    finally:
        _overview_refresh_lock.release()


def emby_library_overview(force=False):
    """片库映射数据三级缓存：内存(5分钟) → 磁盘(立即返回+后台刷新) → 同步构建。
    全量拉取 Emby 分集很慢，磁盘缓存保证每次进页面秒开，后台静默更新。"""
    if not force:
        if _emby_lib_cache['data'] and time.time() - _emby_lib_cache['ts'] < _CACHE_TTL:
            return _emby_lib_cache['data']
        disk = _load_overview_disk()
        if disk is not None:
            _emby_lib_cache['data'] = disk['data']
            _emby_lib_cache['ts'] = disk['ts']
            if time.time() - disk['ts'] >= _CACHE_TTL:
                threading.Thread(target=_overview_bg_refresh, daemon=True,
                                 name='emby-overview-refresh').start()
            return _emby_lib_cache['data']
    out = _build_emby_library_overview()
    _emby_lib_cache['ts'] = time.time()
    _emby_lib_cache['data'] = out
    _save_overview_disk(out)
    return out


_MEDIA_EXT = {'.strm', '.mkv', '.mp4', '.ts', '.m2ts', '.avi', '.mov', '.wmv', '.flv', '.rmvb', '.iso'}


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
        conv = EMBY_PATHS.to_container(d)
        if conv is None:
            dirs[d] = True
            continue
        root = L_ROOT if str(conv).startswith(str(L_ROOT) + os.sep) else S_ROOT
        dirs[d] = True if not root.exists() else _dir_has_media(conv)
    dead = sum(1 for v in dirs.values() if not v)
    if len(dirs) > 20 and dead / len(dirs) > 0.5:
        log.warning('片库映射：%d/%d 个目录检测为空，疑似挂载异常，本次不做幽灵过滤', dead, len(dirs))
        return {}
    return dirs


def _build_emby_library_overview():
    """构建统一媒体身份的片库快照。

    关键口径：双库事实身份优先由「剧名 + 年份」确定，TMDB 只作元数据。
    这样即使 Emby 某条目误写了 TMDB ID，也不会把另一部作品错误合并；
    同一身份的季/集按 ``(season, episode)`` union。
    """
    out = {'series': [], 'movies': []}
    lib_of = EMBY_PATHS.lib_of
    series_data = emby_request('/Items', {
        'Recursive': 'true', 'IncludeItemTypes': 'Series',
        'Fields': 'ProviderIds,Path,ProductionYear,CommunityRating,ImageTags,Genres',
        'Limit': 50000,
    }) or {}
    episodes_all = _fetch_all_episodes()
    alive = _alive_dir_map(ep.get('Path') for ep in episodes_all)

    def _ep_alive(path):
        p = (path or '').replace('\\', '/')
        return alive.get(p.rsplit('/', 1)[0] if '/' in p else '', True)

    # 先把真实分集按 Emby SeriesId 收好，再按 TMDB 身份聚合。
    eps_by_series = defaultdict(list)
    for ep in episodes_all:
        sid = ep.get('SeriesId')
        if sid and _ep_alive(ep.get('Path')):
            eps_by_series[sid].append(ep)

    groups = {}
    for s in series_data.get('Items', []):
        sid = s.get('Id')
        if not sid or not eps_by_series.get(sid):
            continue  # 空壳 Series 不进入片库映射
        tmdb_id = str((s.get('ProviderIds') or {}).get('Tmdb') or '')
        key = f'tv:{tmdb_id}' if tmdb_id else f'id:{sid}'
        g = groups.setdefault(key, {
            'id': sid, 'series_ids': [], 'name': s.get('Name'),
            'year': s.get('ProductionYear'), 'rating': s.get('CommunityRating'),
            'tmdb_id': tmdb_id or None, 'genres': list(s.get('Genres') or []),
            'paths': [], 'libs': set(), 'has_image': False,
            'episodes': set(), 'local_eps': set(), 'share_eps': set(),
        })
        if sid not in g['series_ids']:
            g['series_ids'].append(sid)
        path = s.get('Path', '') or ''
        if path and path not in g['paths']:
            g['paths'].append(path)
        lib = lib_of(path)
        if lib:
            g['libs'].add(lib)
        g['has_image'] = g['has_image'] or ('Primary' in (s.get('ImageTags') or {}))
        if not g.get('name') and s.get('Name'):
            g['name'] = s.get('Name')
        if not g.get('year') and s.get('ProductionYear'):
            g['year'] = s.get('ProductionYear')
        for ep in eps_by_series[sid]:
            sn = ep.get('ParentIndexNumber'); en = ep.get('IndexNumber')
            if sn is None or en is None:
                continue
            try:
                pair = (int(sn), int(en))
            except (TypeError, ValueError):
                continue
            if pair[0] < 0 or pair[1] <= 0:
                continue
            g['episodes'].add(pair)
            ep_lib = lib_of(ep.get('Path', '') or '')
            if ep_lib == 'local': g['local_eps'].add(pair)
            elif ep_lib == 'share': g['share_eps'].add(pair)

    for g in groups.values():
        season_map = defaultdict(set)
        for sn, en in g['episodes']:
            season_map[sn].add(en)
        seasons = []
        total_missing = 0
        for sn in sorted(season_map):
            eps_set = sorted(season_map[sn])
            if not eps_set:
                continue
            lo, hi = eps_set[0], eps_set[-1]
            pre = list(range(1, lo)) if lo > 1 else []
            mid = sorted(set(range(lo, hi + 1)) - set(eps_set))
            miss = sorted(set(pre + mid))
            total_missing += len(miss)
            seasons.append({'season': sn, 'episodes': len(eps_set), 'max_ep': hi,
                            'missing': miss, 'complete': not miss})
        if not seasons:
            continue
        libs = g['libs']
        out['series'].append({
            'id': g['id'], 'series_ids': g['series_ids'], 'name': g['name'],
            'year': g['year'], 'rating': g['rating'], 'tmdb_id': g['tmdb_id'],
            'genres': g['genres'], 'in_local': 'local' in libs, 'in_share': 'share' in libs,
            'path': g['paths'][0] if g['paths'] else '', 'paths': g['paths'],
            'has_image': g['has_image'], 'seasons': seasons,
            'total_seasons': len(seasons), 'total_episodes': len(g['episodes']),
            'local_eps': len(g['local_eps']), 'share_eps': len(g['share_eps']),
            'have_eps': len(g['episodes']), 'missing_eps': total_missing,
            'complete': total_missing == 0 and len(seasons) > 0,
        })

    movie_data = emby_request('/Items', {
        'Recursive': 'true', 'IncludeItemTypes': 'Movie',
        'Fields': 'ProviderIds,Path,ProductionYear,CommunityRating,ImageTags,Genres',
        'Limit': 50000,
    }) or {}
    movie_items = movie_data.get('Items', [])
    m_alive = _alive_dir_map(m.get('Path') for m in movie_items)
    movie_groups = {}
    for m in movie_items:
        path = m.get('Path', '') or ''
        _pp = path.replace('\\', '/')
        if not m_alive.get(_pp.rsplit('/', 1)[0] if '/' in _pp else '', True):
            continue
        tmdb_id = str((m.get('ProviderIds') or {}).get('Tmdb') or '')
        key = f'movie:{tmdb_id}' if tmdb_id else f'id:{m.get("Id")}'
        g = movie_groups.setdefault(key, {
            'id': m.get('Id'), 'ids': [], 'name': m.get('Name'),
            'year': m.get('ProductionYear'), 'rating': m.get('CommunityRating'),
            'tmdb_id': tmdb_id or None, 'genres': list(m.get('Genres') or []),
            'paths': [], 'libs': set(), 'has_image': False,
        })
        if m.get('Id') and m.get('Id') not in g['ids']:
            g['ids'].append(m.get('Id'))
        if path and path not in g['paths']:
            g['paths'].append(path)
        lib = lib_of(path)
        if lib: g['libs'].add(lib)
        g['has_image'] = g['has_image'] or ('Primary' in (m.get('ImageTags') or {}))
    for g in movie_groups.values():
        libs = g['libs']
        out['movies'].append({
            'id': g['id'], 'ids': g['ids'], 'name': g['name'], 'year': g['year'],
            'rating': g['rating'], 'tmdb_id': g['tmdb_id'], 'genres': g['genres'],
            'in_local': 'local' in libs, 'in_share': 'share' in libs,
            'path': g['paths'][0] if g['paths'] else '', 'paths': g['paths'],
            'has_image': g['has_image'],
        })
    return out

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


MANUAL_DONE_FILE = STATE_DIR / 'manual_done.json'


def read_manual_done() -> dict:
    """人工标记「已完结」的剧集：Emby 条目 id → {name, ts}。
    TMDB 季数 / 集数与实际不符（如国产剧只有一季而 TMDB 有很多季）时手动标记，
    仅影响缺集统计与提示，不改动任何文件。由 Web「片库映射」弹窗里的「手动完结」写入。"""
    try:
        data = json.loads(MANUAL_DONE_FILE.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _apply_manual_done_to_series(series):
    """统一应用手动完结标记，并返回不修改原对象的健康统计。"""
    done = read_manual_done()
    rows = []
    for src in series or []:
        s = dict(src)
        ti = dict(s.get('tmdb_info') or {})
        ids = [str(x) for x in (s.get('series_ids') or [s.get('id')]) if x]
        if any(x in done for x in ids) and ti.get('match_status') in ('missing', 'ongoing'):
            ti['match_status'] = 'aligned'
            ti['manual_done'] = True
        s['tmdb_info'] = ti
        s['_md'] = bool(ti.get('manual_done'))
        rows.append(s)
    return rows


def build_library_health_snapshot(max_age=1800):
    """统一媒体健康快照：所有页面/晨报/治理总览使用同一份 TMDB 对照缓存。
    只读缓存，不在页面请求线程里触发全量 Emby/TMDB 扫描；缓存过期由后台任务刷新。"""
    data = read_emby_lib_cache(max_age=None) or {}
    if not data.get('series') and not data.get('movies'):
        data = emby_library_overview(force=False) or {}
    series = _apply_manual_done_to_series(data.get('series') or [])
    movies = data.get('movies') or []
    stats = {'total_series': len(series), 'total_movies': len(movies),
             'aligned': 0, 'missing': 0, 'extra': 0, 'ongoing': 0,
             'unmatched': 0, 'no_tmdb': 0, 'manual_done': 0}
    top = []
    for s in series:
        st = (s.get('tmdb_info') or {}).get('match_status', 'unmatched')
        if st in stats: stats[st] += 1
        if s.get('_md'): stats['manual_done'] += 1
        if st == 'missing':
            ti = s.get('tmdb_info') or {}
            top.append({'name': s.get('name'), 'year': s.get('year'),
                        'diff': abs(int(ti.get('diff') or 0)),
                        'tot': int(ti.get('tmdb_total') or 0),
                        'have': int(s.get('have_eps') or s.get('total_episodes') or 0)})
    top.sort(key=lambda x: x['diff'], reverse=True)
    eps = sum(int(s.get('have_eps') or s.get('total_episodes') or 0) for s in series)
    snapshot = {'schema_version': 1, 'ts': data.get('ts') or 0,
                'series': series, 'movies': movies, 'stats': stats,
                'episodes': eps, 'top_missing': top[:10],
                'source': 'emby_tmdb_cache'}
    return snapshot


def save_library_snapshot(snapshot):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = LIBRARY_SNAPSHOT_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding='utf-8')
        tmp.replace(LIBRARY_SNAPSHOT_FILE)
    except (OSError, TypeError) as e:
        log.warning('统一片库快照写入失败: %s', e)


def load_library_snapshot(max_age=None, background_refresh=True):
    try:
        data = json.loads(LIBRARY_SNAPSHOT_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if max_age is not None and time.time() - float(data.get('ts') or 0) > max_age:
        if background_refresh:
            threading.Thread(target=refresh_library_snapshot_background, daemon=True, name='library-snapshot-refresh').start()
        return data
    return data


def refresh_library_snapshot_background():
    """从已存在的 TMDB 对照缓存生成轻量快照；不触发网络扫描。"""
    try:
        snap = build_library_health_snapshot()
        if snap.get('series') or snap.get('movies'):
            save_library_snapshot(snap)
    except Exception as e:
        log.warning('统一片库快照刷新失败: %s', e)


def unified_health(max_age=1800):
    """统一健康数据入口。优先磁盘快照；过期只返回旧快照并后台刷新，保证 Web 秒开。"""
    snap = load_library_snapshot(max_age=max_age)
    if snap:
        return snap
    snap = build_library_health_snapshot()
    if snap.get('series') or snap.get('movies'):
        save_library_snapshot(snap)
    return snap


def daily_consistency_snapshot(force_refresh: bool = False) -> dict:
    """轻量统一事实快照：晨报、总览、执行记录口径使用同一批缓存与规则指纹。"""
    ingest = refresh_ingest_cache(hours=24) if force_refresh else (read_ingest_cache() or {})
    health = unified_health(max_age=1800) or {}
    gov = load_latest_scan() or {}
    rule_snapshot = _current_rule_snapshot()
    return {
        'schema_version': 2, 'ts': time.time(), 'rule_sig': rule_snapshot['sig'],
        'ingest': {'ts': ingest.get('ts', 0), 'ok': ingest.get('ok', True), 'stats': ingest.get('stats', {}),
                   'warning': ingest.get('stale_error') or ('' if ingest.get('ok', True) else ingest.get('error', ''))},
        'library': {'ts': health.get('ts', 0), 'stats': health.get('stats', {}), 'episode_total': health.get('episode_total', 0),
                    'top_missing': (health.get('top_missing') or [])[:10]},
        'governance': {'ts': gov.get('ts', 0), 'scan_id': gov.get('plan_id', ''), 'result': gov.get('result') or {}},
        'rules': rule_snapshot['rules'],
    }


def gap_report(max_age=30 * 60):
    """缺集检测的统一口径：与网页「片库映射 → 缺集」一致，按 TMDB 对照判断。
    优先读网页同一份对照缓存；缓存过期/不存在时现场对照一次。
    返回 {'missing': [...], 'stats': {...}, 'from_cache': bool, 'cache_ts': float}"""
    data = read_emby_lib_cache(max_age=max_age)
    from_cache = bool(data)
    if not data:
        from argparse import Namespace as _NS
        data = action_emby_library(_NS(force=False, with_tmdb=True))
        if data.get('status') == 'error':
            return {'status': 'error', 'message': data.get('message', '')}
        try:
            save_emby_lib_cache(data)  # 落盘，让网页读到的也是这一份，两边数据一致
            data['ts'] = time.time()
        except Exception:
            pass
    series = _apply_manual_done_to_series(data.get('series') or [])
    save_library_snapshot(build_library_health_snapshot(max_age=max_age))
    stats = {'total': len(series), 'aligned': 0, 'missing': 0, 'extra': 0,
             'ongoing': 0, 'unmatched': 0}
    missing = []
    done = read_manual_done()  # 手动完结的剧不再算缺集 / 在更，与网页「片库映射」口径一致
    for s_ in series:
        st = (s_.get('tmdb_info') or {}).get('match_status', 'unmatched')
        if st == 'no_tmdb': st = 'unmatched'
        ids = [str(x) for x in (s_.get('series_ids') or [s_.get('id')]) if x]
        if any(x in done for x in ids) and st in ('missing', 'ongoing'):
            st = 'aligned'
        if st in stats: stats[st] += 1
        if st == 'missing': missing.append(s_)
    missing.sort(key=lambda x: (x.get('tmdb_info') or {}).get('diff') or 0)  # diff 为负，越小缺得越多
    return {'status': 'success', 'missing': missing, 'stats': stats,
            'movies_total': len(data.get('movies') or []),
            'from_cache': from_cache, 'cache_ts': data.get('ts') or 0}


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
    # 手动完结统一应用到后端快照，避免治理总览/晨报与片库映射各算一套。
    done = read_manual_done()
    for s in series:
        ti = s.get('tmdb_info') or {}
        ids = [str(x) for x in (s.get('series_ids') or [s.get('id')]) if x]
        if any(x in done for x in ids) and ti.get('match_status') in ('missing', 'ongoing'):
            ti = dict(ti)
            ti['match_status'] = 'aligned'
            ti['diff'] = 0
            s['tmdb_info'] = ti
            s['_md'] = True
    stats = {'total_series': len(series), 'total_movies': len(movies),
             'aligned': 0, 'missing': 0, 'extra': 0, 'ongoing': 0, 'unmatched': 0, 'no_tmdb': 0,
             'complete_series': 0, 'incomplete_series': 0}
    for s in series:
        st = (s.get('tmdb_info') or {}).get('match_status', 'unmatched')
        if st in stats: stats[st] += 1
        if s.get('complete') or st == 'aligned': stats['complete_series'] += 1
        else: stats['incomplete_series'] += 1
    result = {'status': 'success', 'stats': stats, 'series': series, 'movies': movies,
              'tmdb_errors': tmdb_errors, 'emby_host': EMBY_HOST}
    if with_tmdb:
        try:
            save_emby_lib_cache(result)
            save_library_snapshot(build_library_health_snapshot())
        except Exception as e:
            log.warning('保存统一片库对照缓存失败: %s', e)
    return result


# ═══════════════════ 入库监控（缓存层） ═══════════════════
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


def _fetch_ingest(hours=24):
    """实际拉取 Emby 近期入库。返回里 `ok=False` 表示至少一项拉取失败（结果不完整，不应覆盖好缓存）。"""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    min_date = cutoff.strftime('%Y-%m-%dT%H:%M:%S.0000000Z')
    tv_tree = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    mov_tree = defaultdict(lambda: defaultdict(list))
    movies_raw = []
    episodes_raw = []
    errors = []

    base = {'Recursive': 'true', 'SortBy': 'DateCreated', 'SortOrder': 'Descending', 'MinDateCreated': min_date}
    try:
        for m in _paged_items(dict(base, IncludeItemTypes='Movie', Fields='DateCreated,Path,Genres,ProviderIds,ProductionYear,Name')):
            dt = parse_dt(m.get('DateCreated'))
            if not dt or dt < cutoff: continue
            movies_raw.append(m)
            n = m.get('Name')
            bucket = mov_tree[_src(m.get('Path', ''))][parse_emby_library(m, True)]
            if n and n not in bucket: bucket.append(n)
    except Exception as e:
        log.warning('入库电影拉取失败: %s', e)
        errors.append('电影: %s' % e)

    try:
        for e in _paged_items(dict(base, IncludeItemTypes='Episode', Fields='DateCreated,Path,SeriesName,Genres,ProviderIds,SeriesId,ParentIndexNumber,IndexNumber,ProductionYear,Name')):
            dt = parse_dt(e.get('DateCreated'))
            if not dt or dt < cutoff: continue
            episodes_raw.append(e)
            tv_tree[_src(e.get('Path', ''))][parse_emby_library(e, False)][e.get('SeriesName') or '未知剧集'] += 1
    except Exception as e:
        log.warning('入库剧集拉取失败: %s', e)
        errors.append('剧集: %s' % e)

    # 统计口径：按媒体身份去重。双库同一 TMDB 电影只算 1 部；剧集按
    # 规范化剧名+年份识别跨库同一剧，再按 (season, episode) union。
    def _ingest_series_key(e):
        name = _normalize_title(str(e.get('SeriesName') or e.get('Name') or '')).casefold()
        year = str(e.get('ProductionYear') or '')
        # Episode 的 ProviderIds 常常是 episode 自身 ID，不能拿它当 Series ID。
        return f"name:{name}|year:{year}" if name else f"sid:{e.get('SeriesId') or ''}"

    movie_ids, movie_fallback = set(), set()
    series_ids, episode_ids = set(), set()
    for m in movies_raw:
        tid = str((m.get('ProviderIds') or {}).get('Tmdb') or '')
        if tid: movie_ids.add(tid)
        else: movie_fallback.add((_normalize_title(str(m.get('Name') or '')).casefold(), str(m.get('ProductionYear') or '')))
    for e in episodes_raw:
        sid = _ingest_series_key(e)
        if sid: series_ids.add(sid)
        try:
            pair = (int(e.get('ParentIndexNumber')), int(e.get('IndexNumber')))
        except (TypeError, ValueError):
            pair = None
        if pair and pair[0] >= 0 and pair[1] > 0 and sid:
            episode_ids.add((sid, pair))
        elif sid:
            # 旧版/测试数据没有季集字段时，退回资源路径去重，避免把统计变成 0。
            episode_ids.add((sid, 'item:' + str(e.get('Path') or e.get('Id') or e.get('Name') or '')))
    total_mov = len(movie_ids) + len(movie_fallback)
    total_series = len(series_ids)
    total_eps = len(episode_ids)

    # 保留原始分类树供详情展示，但 stats 使用身份去重后的数字。
    return {
        'ts': time.time(),
        'hours': hours,
        'ok': not errors,
        'error': '；'.join(errors),
        'stats': {'movies': total_mov, 'series': total_series, 'episodes': total_eps},
        'tree': {
            'tv': {k: {c: dict(s) for c, s in v.items()} for k, v in tv_tree.items()},
            'mov': {k: {c: list(ns) for c, ns in v.items()} for k, v in mov_tree.items()},
        },
        'movies_raw': [{'name': m.get('Name'), 'path': m.get('Path',''), 'created': m.get('DateCreated')} for m in movies_raw[:200]],
        'episodes_raw': [{'name': e.get('Name'), 'series': e.get('SeriesName'), 'path': e.get('Path',''), 'created': e.get('DateCreated')} for e in episodes_raw[:500]],
    }


def refresh_ingest_cache(hours=24) -> dict:
    """立即拉取并覆盖缓存。拉取失败（超时等）时保留上一份完整缓存，只附上失败信息，
    避免把「+0 部 / +0 集」的残缺结果写成最新数据。"""
    log.info('入库缓存刷新开始（%sh）', hours)
    data = _fetch_ingest(hours=hours)
    if not data.get('ok', True):
        old = read_ingest_cache()
        if old and old.get('ok', True):
            old['stale_error'] = data.get('error', '')
            old['stale_error_ts'] = time.time()
            old['from_cache'] = True
            _write_ingest_cache(old)
            log.warning('入库缓存刷新失败，保留旧缓存（%d 秒前）: %s',
                        int(time.time() - old.get('ts', 0)), data.get('error'))
            return old
        # 没有旧缓存可退：仍把失败结果落盘，但带 ok=False，界面据此提示而不是当成「0 新增」
    _write_ingest_cache(data)
    st = data.get('stats', {})
    if not data.get('ok', True):
        log.warning('入库缓存刷新失败且无旧缓存可保留: %s', data.get('error'))
        return data
    log.info('入库缓存刷新完成：电影 %d / 剧集 %d 部 / 集 %d',
             st.get('movies', 0), st.get('series', 0), st.get('episodes', 0))
    return data


def _write_ingest_cache(data):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = INGEST_CACHE_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        tmp.replace(INGEST_CACHE_FILE)
    except OSError as e:
        log.warning('入库缓存写入失败: %s', e)


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
    if cached and cached.get('ok', True):
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
    lib = emby_lib_of(path)
    return '本地影视库' if lib == 'local' else ('分享影视库' if lib == 'share' else '其它库')


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
    warn = data.get('stale_error') or ('' if data.get('ok', True) else data.get('error', ''))
    if not full:
        return {'status': 'success', 'has_more': True,
                'stats': st, 'from_cache': data.get('from_cache', False),
                'cache_ts': data.get('ts', 0), 'ok': data.get('ok', True), 'warning': warn,
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
    if warn:
        rep.insert(1, f'⚠️ 本次扫描失败（{esc(warn)}），以下为旧数据')
    return {'status': 'success', 'text': '\n'.join(rep),
            'ok': data.get('ok', True), 'warning': warn,
            'from_cache': data.get('from_cache', False), 'cache_ts': data.get('ts', 0),
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
    """查 Emby 里某剧（按 tmdb_id）的所有副本并 union 分集。

    同一剧可能同时存在本地/分享两个 Series 条目；旧版 Limit=1 会随机只取一边，
    导致追更把另一边已有的集误报为缺集。1.6.3 对所有匹配 Series 聚合 (季,集)。
    """
    if not series_tmdb_id: return None
    items = []
    try:
        data = emby_request('/Items', {
            'Recursive': 'true', 'IncludeItemTypes': 'Series',
            'Fields': 'ProviderIds,Name,Path', 'Limit': 50000,
            'AnyProviderIdEquals': f'Tmdb.{series_tmdb_id}',
        }) or {}
        items = data.get('Items') or []
    except Exception:
        items = []
    if not items:
        try:
            data = emby_request('/Items', {
                'Recursive': 'true', 'IncludeItemTypes': 'Series',
                'Fields': 'ProviderIds,Name,Path', 'Limit': 50000,
            }) or {}
            items = [it for it in (data.get('Items') or [])
                     if str((it.get('ProviderIds') or {}).get('Tmdb') or '') == str(series_tmdb_id)]
        except Exception:
            return None
    if not items: return None
    have = set(); created = {}; series_ids = []
    names = []
    for series in items:
        sid = series.get('Id')
        if not sid: continue
        series_ids.append(sid)
        if series.get('Name') and series.get('Name') not in names:
            names.append(series.get('Name'))
        try:
            eps = emby_request('/Items', {
                'ParentId': sid, 'Recursive': 'true', 'IncludeItemTypes': 'Episode',
                'Fields': 'ParentIndexNumber,IndexNumber,IndexNumberEnd,DateCreated,Path',
                'Limit': 50000,
            }) or {}
        except Exception:
            continue
        for ep in eps.get('Items') or []:
            try:
                sn = int(ep.get('ParentIndexNumber') or 0)
                en = int(ep.get('IndexNumber') or 0)
                en_end = int(ep.get('IndexNumberEnd') or en)
            except (TypeError, ValueError):
                continue
            if sn <= 0 or en <= 0: continue
            for e in range(en, max(en, en_end) + 1):
                have.add((sn, e))
            created[(sn, en)] = max(created.get((sn, en), ''), ep.get('DateCreated') or '')
    if not have: return None
    sn, en = max(have)
    return {
        'series_id': series_ids[0] if series_ids else None,
        'series_ids': series_ids,
        'series_name': names[0] if names else '',
        'season': sn, 'episode': en,
        'date_created': created.get((sn, en), ''),
        'episodes': have,
    }


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


def _ep_key_num(k):
    m = re.match(r'S(\d+)E(\d+)', k or '')
    return (int(m.group(1)) * 10000 + int(m.group(2))) if m else 0


def check_subscriptions(send_notify=True) -> dict:
    """
    改进版：
      1. 拿 Emby 已有集 + 最大集号（新集判定）
      2. 拿 TMDB 已播集，与 Emby 已有集逐集对照（缺集判定）
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

        # 新集判定：只认比上次更大的集号；latest_ep 只进不退（下架 / 删集不回退）
        if prev_key and cur_key and cur_key != prev_key:
            if _ep_key_num(cur_key) > _ep_key_num(prev_key):
                new_ep_update = {'old': prev_key, 'new': cur_key}
            else:
                cur_key = prev_key

        # 缺集判定：TMDB 已播集逐集对照 Emby 已有集，Emby 多出来的集不抵扣缺口
        if tmdb_info and latest:
            aired = tmdb_info.get('aired') or set()
            have = latest.get('episodes') or set()
            lack = aired - have
            if lack:
                missing_update = {
                    'tmdb_total': len(aired),
                    # emby_total 取「已播范围内 Emby 有的集数」，保证 tmdb_total - emby_total == diff
                    'emby_total': len(aired & have),
                    'diff': len(lack),
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
        lines = [tg_title('🔔', '追更订阅', f'{len(updates)} 部有变化')]
        for u in updates:
            lines.append('')
            lines.append(f"📺 <b>《{html.escape(str(u['name'] or ''))}》</b>")
            if u.get('new_ep'):
                lines.append(f"　🆕 新集入库 {u['new_ep']['old']} → <b>{u['new_ep']['new']}</b>")
            if u.get('missing'):
                m = u['missing']
                lines.append(f"　⚠️ 缺 <b>{m['diff']}</b> 集（TMDB 已播 {m['tmdb_total']}）")
        notify_telegram('\n'.join(lines))

    return {'updates': updates, 'total': len(subs)}


# ═══════════════════ 晨报 ═══════════════════
MORNING_GAP_TOP = 50   # 晨报里最多列出多少部缺集剧（按缺得最多排序），完整清单看 Web


def build_morning_report(items: list, force_refresh: bool = False) -> str:
    now = datetime.datetime.now()
    lines = [tg_title('☀️', 'TTD Guard 晨报', f'{now:%Y-%m-%d} 周{_WEEK[now.weekday()]}')]
    snap = daily_consistency_snapshot(force_refresh=force_refresh)
    lines.append(f"🧭 <i>统一快照 · 规则 {html.escape(str(snap.get('rule_sig') or ''))}</i>")

    if 'stats' in items:
        try:
            st = (snap.get('ingest') or {}).get('stats') or {}
            lines += ['', '📊 <b>近 24 小时入库</b>',
                      f"🎬 电影　<b>+{st.get('movies', 0)}</b> 部",
                      f"📺 剧集　<b>+{st.get('series', 0)}</b> 部 · <b>+{st.get('episodes', 0)}</b> 集"]
        except Exception as e:
            lines += ['', f'📊 入库统计失败: {html.escape(str(e))}']

    if 'subscriptions' in items:
        try:
            r = check_subscriptions(send_notify=False)
            lines += ['', f"🔔 <b>订阅更新</b>　<i>{r.get('total', 0)} 部订阅</i>"]
            ups = r.get('updates') or []
            if ups:
                for u in ups[:10]:
                    name = html.escape(str(u.get('name') or ''))
                    if u.get('new_ep'):
                        lines.append(f"📺 《{name}》 {u['new_ep']['old']} → <b>{u['new_ep']['new']}</b>")
                    if u.get('missing'):
                        lines.append(f"⚠️ 《{name}》缺 <b>{u['missing']['diff']}</b> 集")
                if len(ups) > 10:
                    lines.append(f'<i>…另有 {len(ups) - 10} 部有变化</i>')
            else:
                lines.append('✅ 无变化')
        except Exception as e:
            lines += ['', f'🔔 订阅检查失败: {html.escape(str(e))}']

    if 'emby_gap' in items:
        try:
            rep = gap_report()
            if rep.get('status') == 'error':
                raise RuntimeError(rep.get('message'))

            def _diff(x):
                return abs((x.get('tmdb_info') or {}).get('diff') or 0)
            broken = sorted(rep['missing'], key=_diff, reverse=True)
            total_gap = sum(_diff(x) for x in broken)
            lines += ['', f"🧩 <b>Emby 缺集</b>　<i>{len(broken)} 部 · 共缺 {total_gap} 集</i>"]
            if broken:
                shown = broken[:MORNING_GAP_TOP]
                # 折叠引用：默认只露前几行（缺得最多的排最前），点一下展开，再点收起
                lines.append('<blockquote expandable>' + '\n'.join(
                    f"• 《{html.escape(str(x.get('name') or ''))}》缺 <b>{_diff(x)}</b> 集" for x in shown)
                    + '</blockquote>')
                if len(broken) > len(shown):
                    lines.append(f'<i>…另有 {len(broken) - len(shown)} 部，完整清单见 Web「片库映射」</i>')
            else:
                lines.append('✅ 全部对齐')
        except Exception as e:
            lines += ['', f'🧩 Emby 检查失败: {html.escape(str(e))}']

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


def _live_series_episodes(series_id: str):
    """某剧当前「真实存在」的分集：Emby 返回的分集里，路径能映射到容器内且文件已不在磁盘上的，
    视为 Emby 尚未清理的残留，直接剔除。删除后 Emby 的刷新是异步的，
    如果直接信 Emby，刚删完的剧集还会被当成\"仍在库里\"，海报就不会消失。
    路径映射不上的（其它库）无法核实，保留。"""
    items = (emby_request('/Items', {
        'ParentId': series_id, 'Recursive': 'true',
        'IncludeItemTypes': 'Episode',
        'Fields': 'Path,ParentIndexNumber,IndexNumber', 'Limit': 5000,
    }) or {}).get('Items') or []
    live = []
    for e in items:
        conv = emby_path_to_container(e.get('Path') or '')
        if conv is not None:
            try:
                if not conv.exists():
                    continue
            except OSError:
                pass
        live.append(e)
    return live


def _resync_series_entry(entry: dict, live: list):
    """用真实存在的分集重算缓存里这部剧的分集统计，并按原 TMDB 数据重新判定缺集。"""
    lib_of = EMBY_PATHS.lib_of
    season_map, local_set, share_set = {}, set(), set()
    for ep in live:
        sn, en = ep.get('ParentIndexNumber'), ep.get('IndexNumber')
        if sn is None or en is None:
            continue
        season_map.setdefault(sn, set()).add(en)
        lib = lib_of(ep.get('Path') or '')
        if lib == 'local':
            local_set.add((sn, en))
        elif lib == 'share':
            share_set.add((sn, en))
    seasons, total_missing = [], 0
    for sn in sorted(season_map):
        eps_set = sorted(season_map[sn])
        lo, hi = eps_set[0], eps_set[-1]
        miss = sorted(set(range(1, lo)) | (set(range(lo, hi + 1)) - set(eps_set)))
        total_missing += len(miss)
        seasons.append({'season': sn, 'episodes': len(eps_set), 'max_ep': hi,
                        'missing': miss, 'complete': not miss})
    entry.update({
        'in_local': bool(local_set), 'in_share': bool(share_set),
        'seasons': seasons, 'total_seasons': len(seasons),
        'total_episodes': sum(x['episodes'] for x in seasons),
        'local_eps': len(local_set), 'share_eps': len(share_set),
        'have_eps': len(local_set | share_set),
        'missing_eps': total_missing,
        'complete': total_missing == 0 and len(seasons) > 0,
    })
    old = entry.get('tmdb_info')
    if old:
        if old.get('tmdb_total'):
            fake = {'status': old.get('tmdb_status') or '',
                    'seasons': [{'season_number': se['season'], 'episode_count': se['tmdb']}
                                for se in (old.get('seasons') or []) if se.get('tmdb')]}
            entry['tmdb_info'] = classify_series_by_tmdb(seasons, fake)
        else:
            old['local_total'] = entry['total_episodes']


def _patch_all_caches(patch):
    """对 内存 / 总览磁盘 / TMDB 对照磁盘 三份缓存各调用一次 patch(data)->bool，改动了就落盘（保留原时间戳）。"""
    if _emby_lib_cache.get('data'):
        patch(_emby_lib_cache['data'])
    disk = _load_overview_disk()
    if disk and patch(disk['data']):
        _save_overview_disk(disk['data'])
    tmdb_cache = read_emby_lib_cache()
    if tmdb_cache and patch(tmdb_cache):
        save_emby_lib_cache(tmdb_cache, keep_ts=True)


def patch_emby_lib_cache_after_series_delete(series_id: str, deleted_target: str = ''):
    """删除 / 同步后就地修补缓存里这部剧（以磁盘真实文件为准）。
    返回 {'remaining': 剩余分集数, 'entry': 新条目或 None, 'series_path': Emby 侧剧目录}。"""
    live = _live_series_episodes(series_id)
    info = {'remaining': len(live), 'entry': None, 'series_path': ''}

    def patch(data):
        series_list = (data or {}).get('series') or []
        idx = next((i for i, x in enumerate(series_list) if x.get('id') == series_id), None)
        if idx is None:
            return False
        info['series_path'] = series_list[idx].get('path') or info['series_path']
        if not live:
            series_list.pop(idx)
        else:
            _resync_series_entry(series_list[idx], live)
            info['entry'] = series_list[idx]
        data['series'] = series_list
        if isinstance(data.get('stats'), dict):
            data['stats']['total_series'] = len(series_list)
        return True

    _patch_all_caches(patch)
    return info


def patch_emby_lib_cache_after_movie_delete(tmdb_id: str, target: str):
    """按 tmdb_id 删电影后，把缓存里「位于被删库」的电影条目移除。返回移除数。"""
    flag = 'in_local' if target == 'local' else 'in_share'
    removed = [0]

    def patch(data):
        movies = (data or {}).get('movies') or []
        keep = [m for m in movies if not (str(m.get('tmdb_id') or '') == str(tmdb_id) and m.get(flag))]
        if len(keep) == len(movies):
            return False
        removed[0] = max(removed[0], len(movies) - len(keep))
        data['movies'] = keep
        if isinstance(data.get('stats'), dict):
            data['stats']['total_movies'] = len(keep)
        return True

    _patch_all_caches(patch)
    return removed[0]


def save_emby_lib_cache(data: dict, keep_ts: bool = False):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    if not (keep_ts and data.get('ts')):
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


_tmdb_scan_lock = threading.Lock()


def refresh_tmdb_scan():
    """后台跑一次完整 TMDB 对照并落盘（带进度上报）。单飞：网页「强制对照」和定时预热
    可能前后脚触发，已在跑时直接返回，不再并发两份全量对照。"""
    if not _tmdb_scan_lock.acquire(blocking=False):
        return {'status': 'running', 'message': 'TMDB 对照已在后台运行'}
    try:
        return _refresh_tmdb_scan()
    finally:
        _tmdb_scan_lock.release()


def _refresh_tmdb_scan():
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
        save_library_snapshot(build_library_health_snapshot())
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
    """扫描双库，返回命中白名单的条目。
    口径与 build_plan 一致：剧名、所在目录、文件路径任一命中都算；
    同一个命中目录（如「百家讲坛」）下的多个 Season 目录聚合成一条。"""
    kws = _exempt_keywords()
    if not kws:
        return []
    results = {}

    def hit_of(disp, files):
        h = _exempt_hit(disp)
        if h:
            return h
        for f in files:
            h = _exempt_hit(str(f))
            if h:
                return h
        return []

    for root, lib_name in ((L_ROOT, '本地'), (S_ROOT, '分享')):
        if not root.exists():
            continue
        lib = _get_lib(root)
        entries = []  # (key, [files], 季号或None)
        for key, files in lib.mov.items():
            entries.append((key, files, None))
        for key, seasons in lib.tv.items():
            for sn, files in seasons.items():
                entries.append((key, files, sn))
        for key, files, sn in entries:
            disp = lib.meta[key][0]
            hits = hit_of(disp, files)
            if not hits:
                continue
            gname = None
            for f in files[:200]:
                gname, _k = _exempt_group_of(f, kws)
                if gname:
                    break
            gname = gname or disp
            r = results.setdefault(gname, {
                'title': gname, 'keyword': hits[0], 'libs': [],
                'members': set(), 'seasons': set(), 'strm': 0})
            if lib_name not in r['libs']:
                r['libs'].append(lib_name)
            r['members'].add(disp)
            if sn is not None:
                r['seasons'].add(sn)
            r['strm'] += len(files)
    out = []
    for r in results.values():
        members = sorted(r['members'], key=_nat_key)
        out.append({
            'title': r['title'], 'keyword': r['keyword'], 'libs': r['libs'],
            # 只有单个节目时季号才有意义；多个 Season 目录时用 members 展示
            'seasons': sorted(r['seasons']) if len(members) == 1 else [],
            'member_count': len(members), 'members': members[:300],
            'strm_count': r['strm'],
        })
    out.sort(key=lambda x: _nat_key(x['title']))
    return out



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
    now = time.time()
    _lib_stats_cache['ts'] = now
    _lib_stats_cache['data'] = result
    # 同一次遍历的总数同步写入 STRM 计数缓存（内存+磁盘），两处数字永远一致
    _strm_count_cache.update({'ts': now, 'local': out['local_total'], 'share': out['share_total']})
    _save_strm_count_disk(out['local_total'], out['share_total'])
    return result


def action_library_stats(args):
    # 缓存命中则直接返回
    now = time.time()
    if _lib_stats_cache['data'] is not None and (now - _lib_stats_cache['ts']) < _CACHE_TTL:
        return _lib_stats_cache['data']
    return _recompute_all_stats()



def emby_path_to_container(emby_path):
    """将 Emby 返回的 Path 转成容器内路径"""
    # 严格按配置的根目录前缀映射（不做末级目录名兜底），宁可对不上也不能删错库
    return EMBY_PATHS.to_container(emby_path)



# ============ 性能缓存（5 分钟 TTL）============
_strm_count_cache = {'ts': 0, 'local': 0, 'share': 0}
_lib_stats_cache = {'ts': 0, 'data': None}
_CACHE_TTL = 300


_STRM_COUNT_CACHE_FILE = STATE_DIR / 'strm_count_cache.json'
_strm_count_refreshing = threading.Lock()


def _load_strm_count_disk():
    try:
        data = json.loads(_STRM_COUNT_CACHE_FILE.read_text(encoding='utf-8'))
        return int(data.get('local', 0)), int(data.get('share', 0)), float(data.get('ts', 0))
    except (OSError, ValueError):
        return None


def _save_strm_count_disk(local, share):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _STRM_COUNT_CACHE_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps({'ts': time.time(), 'local': local, 'share': share}),
                       encoding='utf-8')
        tmp.replace(_STRM_COUNT_CACHE_FILE)
    except OSError as e:
        log.warning('STRM 计数缓存写入失败: %s', e)


def _strm_count_bg_refresh():
    """后台重算统计（单飞）。走 _recompute_all_stats 统一遍历，
    同时更新分类统计与 STRM 计数，保证两处数字一致。"""
    if not _strm_count_refreshing.acquire(blocking=False):
        return
    try:
        _recompute_all_stats()
        log.info('后台刷新统计完成: local=%d share=%d',
                 _strm_count_cache['local'], _strm_count_cache['share'])
    except Exception as e:
        log.warning('统计后台刷新失败: %s', e)
    finally:
        _strm_count_refreshing.release()


def _get_strm_counts():
    """STRM 总数三级缓存：内存(5分钟) → 磁盘(立即返回+后台刷新) → 同步首算。
    大库 rglob 全量遍历很慢，磁盘缓存保证总览页秒开，后台静默刷新。"""
    now = time.time()
    if now - _strm_count_cache['ts'] < _CACHE_TTL and _strm_count_cache['ts'] > 0:
        return _strm_count_cache['local'], _strm_count_cache['share']
    disk = _load_strm_count_disk()
    if disk is not None:
        l, s_, ts = disk
        _strm_count_cache.update({'ts': ts or (now - _CACHE_TTL), 'local': l, 'share': s_})
        if time.time() - _strm_count_cache['ts'] >= _CACHE_TTL:
            threading.Thread(target=_strm_count_bg_refresh, daemon=True,
                             name='strm-count-refresh').start()
        return _strm_count_cache['local'], _strm_count_cache['share']
    # 首次无任何缓存：同步算一次并落盘
    l = sum(1 for _ in L_ROOT.rglob('*.strm')) if L_ROOT.exists() else 0
    s_ = sum(1 for _ in S_ROOT.rglob('*.strm')) if S_ROOT.exists() else 0
    _strm_count_cache.update({'ts': now, 'local': l, 'share': s_})
    _save_strm_count_disk(l, s_)
    log.info('缓存刷新 STRM 计数: local=%d share=%d', l, s_)
    return l, s_


def invalidate_stats_cache():
    """清空统计缓存（删除后调用）"""
    _strm_count_cache['ts'] = 0
    _lib_stats_cache['ts'] = 0
    _lib_stats_cache['data'] = None


def invalidate_media_caches(keep_emby_lib=False):
    """文件变动后统一失效内存缓存（分集 / Emby 索引 / Lib 快照 / 统计）。
    keep_emby_lib=True 时保留片库映射缓存（单剧删除会就地修补它，避免整页重跑 TMDB 对照）。"""
    _ep_cache['ts'] = 0
    _ep_cache['data'] = None
    _emby_index_cache['ts'] = 0
    _emby_index_cache['data'] = None
    if not keep_emby_lib:
        _emby_lib_cache['ts'] = 0
        _emby_lib_cache['data'] = None
    _invalidate_lib_cache()
    invalidate_stats_cache()


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
    'clean_orphans': action_clean_orphan_dirs,
}
MUTATING = {'inter_clean', 'clean_orphans'}


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