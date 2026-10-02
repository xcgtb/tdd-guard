# -*- coding: utf-8 -*-
"""
media_agent.core —— 纯函数层
设计约束：不做任何 IO，不读取环境变量，给定相同输入必然返回相同输出。
"""
import re
from functools import lru_cache
from typing import Iterable, Optional, Sequence, Tuple

# ═══════════════════ 正则常量 ═══════════════════
RE_SEASON_DIR = re.compile(r'(?i)^(?:season[ ._-]*(\d{1,3})|s(\d{1,3})|第\s*(\d{1,3})\s*季)(?![0-9a-z])')
RE_SPECIAL_DIR = re.compile(r'(?i)^(?:specials?|特别篇|特典)(?![0-9a-z])')
RE_SXXEXX = re.compile(r'(?i)(?<![a-z0-9])s(\d{1,2})[ ._-]*e(?:p)?[ ._-]*(\d{1,4})(?!\d)')
RE_EP = re.compile(r'(?i)(?<![a-z0-9])(?:ep?|sp)[ ._-]*(\d{1,3})(?!\d)|第\s*(\d{1,4})\s*[集话話期]')
RE_SP_NAME = re.compile(r'(?i)特别篇|(?<![a-z0-9])sp[ ._-]*\d+(?![a-z0-9])')
RE_TMDB = re.compile(r'(?i)tmdb(?:id)?[=\-: ]*(\d+)')
RE_BRACKET = re.compile(r'\[.*?\]|\{.*?\}|\(.*?\)')
RE_YEAR = re.compile(r'\((\d{4})\)')

RE_DV = re.compile(r'(?<![A-Z0-9])(?:DV|DOVI)(?![A-Z0-9])|DOLBY[ ._-]?VISION')
RE_4K = re.compile(r'2160P|(?<![A-Z0-9])4K(?![A-Z0-9])')
RE_FPS = re.compile(r'(?<![0-9])(?:60|120)FPS')
RE_HDR = re.compile(r'(?<![A-Z0-9])HDR(?:10)?(?![A-Z0-9])')
RE_SDR = re.compile(r'(?<![A-Z0-9])SDR(?![A-Z0-9])')
RE_BIT10 = re.compile(r'(?i)10[ ._-]?bit')

# ═══════════════════ 默认数据 ═══════════════════
DEFAULT_EXEMPT_KEYWORDS: Tuple[str, ...] = ()
DEFAULT_CATEGORIES: Tuple[str, ...] = (
    '👶 儿童节目', '🎤 演唱会', '🎪 综艺剧', '⛩️ 动漫剧', '🧸 动画电影', '📽️ 纪录片',
    '🇨🇳 国产剧', '🇺🇸 欧美剧', '🇯🇵 日韩剧', '🇨🇳 华语电影', '🌐 外语电影',
)
DEFAULT_KIDS_KW: Tuple[str, ...] = (
    '儿童', '少儿', '亲子', '早教', '朵拉', '佩奇', 'jojo', '布鲁伊', '熊出没',
    '喜羊羊', '贝瓦', '汪汪队', '宝宝巴士', '神厨小福贵', '虹猫蓝兔', '神兵小将',
    '葫芦兄弟', '大头儿子',
)

# ═══════════════════ 文本处理 ═══════════════════
def esc(s) -> str:
    """Markdown 转义"""
    return re.sub(r'([_*`\[])', r'\\\1', str(s))

@lru_cache(maxsize=8192)
def parse_season_dir(name: str) -> Optional[int]:
    """解析季目录名"""
    name = name.strip()
    m = RE_SEASON_DIR.match(name)
    if m:
        return int(next(g for g in m.groups() if g is not None))
    if RE_SPECIAL_DIR.match(name):
        return 0
    return None

def get_ep(name: str, parent_name: str = '', allow_bare_ep: bool = True) -> Optional[Tuple[int, int]]:
    """从文件名+父目录推断 (季, 集)。

    ``allow_bare_ep`` 只允许在明确的剧集上下文中打开。
    裸 ``EP04`` / ``Episode 4`` 本身并不足以证明文件是电视剧；电影
    （例如 ``Star Wars Ep 4``）不应因此被错误归入 S01E04。
    SxxExx、季目录和 Specials 仍是强证据。
    """
    sd = parse_season_dir(parent_name) if parent_name else None
    m = RE_SXXEXX.search(name)
    if m:
        return int(m.group(1)), int(m.group(2))
    em = RE_EP.search(name)
    ep = int(em.group(1) or em.group(2)) if em else None
    if sd == 0 or RE_SP_NAME.search(name):
        return 0, (ep if ep is not None else 1)
    if sd is not None:
        return (sd, ep) if ep is not None else None
    if allow_bare_ep and ep is not None:
        return 1, ep
    return None

@lru_cache(maxsize=16384)
def title_key(folder: str) -> Tuple[str, str, str, Optional[str]]:
    """返回历史兼容的 (key, 展示名, base, 年份)。

    注意：这里保留 TMDB key 语义给旧接口/工具使用；双库治理不得把 TMDB
    编号当作文件系统事实身份，治理侧使用 :func:`governance_title_key`。
    """
    t = RE_TMDB.search(folder)
    y = RE_YEAR.search(folder)
    year = y.group(1) if y else None
    base = re.sub(r'\s+', ' ', RE_BRACKET.sub('', folder)).strip() or folder
    disp = base + (f' ({year})' if year else '')
    return (f'tmdb:{t.group(1)}' if t else disp), disp, base, year


@lru_cache(maxsize=16384)
def governance_title_key(folder: str) -> Tuple[str, str, str, Optional[str]]:
    """双库治理的真实媒体身份：剧名 + 年份。

    TMDB 只是第三方元数据，Emby/整理工具把错误的 TMDB ID 写进目录名时，
    不能因此把《A》与《B》合并，更不能因此产生跨片治理动作。治理扫描
    必须先以用户可见的目录剧名和年份建立身份；TMDB ID 仅作为辅助信息。
    """
    y = RE_YEAR.search(folder)
    year = y.group(1) if y else None
    base = re.sub(r'\s+', ' ', RE_BRACKET.sub('', folder)).strip() or folder
    norm = re.sub(r'[._:\-]+', ' ', base)
    norm = re.sub(r'\s+', ' ', norm).strip().casefold()
    key = f'title:{norm}|year:{year or ""}'
    disp = base + (f' ({year})' if year else '')
    return key, disp, base, year

# ═══════════════════ 画质评分 ═══════════════════
# 画质分层比较，避免「1080p DV > 4K HDR」这类跨分辨率错判。
# 比较维度优先级（高 → 低）：
#   1. 分辨率：2160p > 1080p > 720p > 其他
#   2. HDR 维度（同分辨率才有意义，作为第二比较位）：DV > HDR10/HDR > SDR > 无
#   3. 编码档次：REMUX > WEB-DL/其他
#   4. 帧率：60fps+ > 其他
#   5. 色深：10bit > 8bit
# 每个维度独立成一位，返回可比较的 tuple；逐位比较保证同级才往后比。

def quality_of(name: str, dolby_first: bool = False) -> Tuple[int, int, int, int, int]:
    """解析文件名，返回五元组，逐位越大越优。

    默认顺序 (分辨率, HDR, 编码, 帧率, 色深) —— 分辨率优先；
    ``dolby_first=True`` 时 (HDR, 分辨率, 编码, 帧率, 色深) —— 杜比优先，
    对齐上游 TgtoDrive 的「杜比优先」洗版覆盖策略（1080p 杜比 > 4K SDR）。
    """
    s = name.upper()
    if RE_4K.search(s):
        res = 3
    elif '1080P' in s:
        res = 2
    elif '720P' in s:
        res = 1
    else:
        res = 0
    if RE_DV.search(s):
        hdr = 3
    elif RE_HDR.search(s):
        hdr = 2
    elif RE_SDR.search(s):
        hdr = 1
    else:
        hdr = 0
    codec = 1 if 'REMUX' in s else 0
    fps = 1 if RE_FPS.search(s) else 0
    bit = 1 if RE_BIT10.search(s) else 0
    if dolby_first:
        return (hdr, res, codec, fps, bit)
    return (res, hdr, codec, fps, bit)


def quality_label(name: str) -> str:
    """人类可读的画质档位标签（用于展示/审计）。"""
    res, hdr, codec, fps, bit = quality_of(name)
    parts = []
    parts.append({3: '2160p', 2: '1080p', 1: '720p'}.get(res, 'SD'))
    if hdr == 3:
        parts.append('DV')
    elif hdr == 2:
        parts.append('HDR')
    if codec:
        parts.append('REMUX')
    return ' '.join(parts)


def get_score(name: str, dolby_first: bool = False) -> Tuple[int, int, int, int, int]:
    """兼容旧调用：返回画质五元组（可比较）。"""
    return quality_of(name, dolby_first=dolby_first)


def best_score(names: Iterable[str], dolby_first: bool = False):
    """取一组文件中的最高画质五元组。"""
    return max((quality_of(n, dolby_first=dolby_first) for n in names), default=(0, 0, 0, 0, 0))


def share_wins(s_q, l_q, tie_keep_local: bool = False) -> bool:
    """比较两组画质元组：s_q 是否胜出（>= 或 >，取决于平局策略）。"""
    return s_q > l_q if tie_keep_local else s_q >= l_q

# ═══════════════════ 集数判定 ═══════════════════
def is_exempt(name: str, keywords: Optional[Sequence[str]] = None) -> bool:
    if keywords is None: keywords = DEFAULT_EXEMPT_KEYWORDS
    return any(k in str(name) for k in keywords)

def fmt_nums(nums: Sequence[int]) -> str:
    if not nums: return ''
    ranges, start, prev = [], nums[0], nums[0]
    for n in nums[1:]:
        if n == prev + 1: prev = n
        else:
            ranges.append(f'E{start:02d}-E{prev:02d}' if start != prev else f'E{start:02d}')
            start = prev = n
    ranges.append(f'E{start:02d}-E{prev:02d}' if start != prev else f'E{start:02d}')
    return ','.join(ranges)

def analyze_season_episodes(eps: Iterable[int], name: str = '', exempt_keywords: Optional[Sequence[str]] = None) -> Tuple[bool, str]:
    eps = list(eps)
    if not eps: return False, '空剧集'
    if is_exempt(name, exempt_keywords): return True, '白名单豁免'
    se = sorted(set(eps))
    lo, hi = se[0], se[-1]
    pre = list(range(1, lo)) if lo > 1 else []
    mid = sorted(set(range(lo, hi + 1)) - set(se))
    if not pre and not mid: return True, '完整'
    reasons = []
    if pre: reasons.append(f'缺失前序 (缺 {fmt_nums(pre)})')
    if mid: reasons.append(f'中间断层 (缺 {fmt_nums(mid)}，疑似被和谐)')
    return False, ' 且 '.join(reasons)

def is_seq(eps: Iterable[int], name: str = '', exempt_keywords: Optional[Sequence[str]] = None) -> bool:
    valid, _ = analyze_season_episodes(eps, name, exempt_keywords)
    return valid

# ═══════════════════ Emby 分类 ═══════════════════
def parse_emby_library(item: dict, is_movie: bool = False, categories: Sequence[str] = DEFAULT_CATEGORIES, kids_keywords: Sequence[str] = DEFAULT_KIDS_KW) -> str:
    path = item.get('Path', '') or ''
    for c in categories:
        parts = c.split(' ', 1)
        if len(parts) > 1 and parts[1] in path: return c
    name = item.get('SeriesName') or item.get('Name') or ''
    text = f"{path} {name} {' '.join(item.get('Genres', []))}".lower()
    if any(k in text for k in kids_keywords): return categories[0]
    if any(k in text for k in ['演唱会', '音乐会', 'concert', '左麟右李']) or re.search(r'\blive\b', text): return categories[1]
    if any(k in text for k in ['综艺', '真人秀', 'talk show', '乘风破浪', '奔跑吧']) or re.search(r'\breality\b', text): return categories[2]
    if any(k in text for k in ['动漫', '国漫', '日漫', 'anime', '动画', 'animation']): return categories[4] if is_movie else categories[3]
    if any(k in text for k in ['纪录', 'documentary', '纪实']): return categories[5]
    if not is_movie:
        if any(k in path for k in ['欧美', '美剧', '英剧']): return categories[7]
        if any(k in path for k in ['日韩', '日本', '韩剧', '韩国']): return categories[8]
        return categories[6]
    return categories[9] if any(k in path for k in ['华语', '国产', '大陆', '港台']) else categories[10]

# ═══════════════════ 洗版覆盖策略引擎（对齐上游 TgtoDrive 自定义覆盖策略） ═══════════════════
COVER_RULE_ORDER = ['release_group', 'source', 'resolution', 'dolby',
                    'bitdepth', 'audio', 'fps', 'filesize']

COVER_RULE_META = {
    'release_group': {'label': '发布组优先级',
                      'desc': '按你维护的发布组列表比较；越靠前优先级越高。'},
    'source': {'label': '资源类型优先级',
               'desc': '识别 UHD/蓝光/WEB 等片源类型；其他或未识别格式默认排在最后。'},
    'resolution': {'label': '分辨率优先级',
                   'desc': '按画面高度比较；2K/1440p 作为兼容档位保留，未识别分辨率排在最后。'},
    'dolby': {'label': '动态范围优先级',
              'desc': 'Dolby Vision 会细分 P7、P5、P8，并识别 HDR Vivid；未识别格式排在最后。'},
    'bitdepth': {'label': '色深优先级',
                 'desc': '优先识别文件名中的 bit depth；未识别色深排在最后。'},
    'audio': {'label': '音频规格优先级',
              'desc': '同一文件有多个音轨时，按其中最高规格参与比较；Audio Vivid 与未识别格式均有明确档位。'},
    'fps': {'label': '帧率优先级',
            'desc': '59.94 / 29.97 / 23.976 会分别归入 60 / 30 / 24 档，未识别帧率排在最后。'},
    'filesize': {'label': '文件大小优先级',
                 'desc': '建议保留在最后，作为所有规格相同或未知时的兜底。（STRM 模式取不到云端真实大小，建议关闭）'},
}

COVER_TIERS_DEFAULT = {
    'source': ['UHD Remux', 'Remux', 'UHD BluRay / BDMV', 'BluRay', 'WEB-DL', 'WEBRip', 'HDTV', 'DVD', '其他 / 未识别'],
    'resolution': ['4320p / 8K', '2160p / 4K', '1440p / 2K', '1080p', '720p', 'SD（576p 及以下）', '其他 / 未识别'],
    'dolby': ['Dolby Vision Profile 7', 'Dolby Vision Profile 5', 'Dolby Vision Profile 8',
              'Dolby Vision（未识别 Profile）', 'HDR10+', 'HDR Vivid', 'HDR10', 'HDR / HLG', 'SDR', '其他 / 未识别'],
    'bitdepth': ['12bit', '10bit', '8bit', '其他 / 未识别'],
    'audio': ['TrueHD Atmos / DTS:X', 'TrueHD / DTS-HD MA', 'LPCM / FLAC', 'Audio Vivid / AV3A',
              'DD+ Atmos', 'DD+ / E-AC-3', 'DTS', 'AC3 / DD', 'AAC', '其他 / 未识别'],
    'fps': ['60 / 59.94 fps', '50 fps', '30 / 29.97 fps', '25 fps', '24 / 23.976 fps', '其他 / 未识别'],
    'filesize': ['大文件优先', '小文件优先'],
    'release_group': [],
}

COVER_ENABLED_DEFAULT = {
    'release_group': False, 'source': True, 'resolution': True, 'dolby': True,
    'bitdepth': True, 'audio': True, 'fps': True, 'filesize': False,
}


def cover_default_strategy():
    """默认覆盖策略：顺序/开关/档位全部对齐上游 TgtoDrive 新增的自定义覆盖策略。"""
    rules = []
    for k in COVER_RULE_ORDER:
        rules.append({'key': k, 'label': COVER_RULE_META[k]['label'], 'desc': COVER_RULE_META[k]['desc'],
                      'enabled': COVER_ENABLED_DEFAULT.get(k, True),
                      'tiers': list(COVER_TIERS_DEFAULT.get(k, [])), 'groups': []})
    return {'allow_wash': True, 'rules': rules}


def _dim_source(s):
    su = s.upper().replace(' ', '')
    is_uhd = '2160' in su or 'UHD' in su
    if 'REMUX' in su:
        return 'UHD Remux' if is_uhd else 'Remux'
    if 'BLURAY' in su or 'BLU-RAY' in su or 'BDMV' in su:
        return 'UHD BluRay / BDMV' if is_uhd else 'BluRay'
    if 'WEB-DL' in su:
        return 'WEB-DL'
    if 'WEBRIP' in su:
        return 'WEBRip'
    if 'HDTV' in su:
        return 'HDTV'
    if 'DVD' in su:
        return 'DVD'
    return '其他 / 未识别'


def _dim_resolution(s):
    su = s.upper()
    if '4320' in su or '8K' in su: return '4320p / 8K'
    if '2160' in su or '4K' in su: return '2160p / 4K'
    if '1440' in su or '2K' in su: return '1440p / 2K'
    if '1080' in su: return '1080p'
    if '720' in su: return '720p'
    if '576' in su or '480' in su: return 'SD（576p 及以下）'
    return '其他 / 未识别'


def _dim_dolby(s):
    su = s.upper()
    if RE_DV.search(su):
        m = re.search(r'(?:DOVI|DOLBY[ ._-]?VISION|DV)[ ._-]*P([578])', su)
        if m:
            return f'Dolby Vision Profile {m.group(1)}'
        return 'Dolby Vision（未识别 Profile）'
    if 'HDR10+' in su: return 'HDR10+'
    if 'HDRVIVID' in su.replace(' ', '') or 'HDR VIVID' in su: return 'HDR Vivid'
    if 'HDR10' in su: return 'HDR10'
    if 'HDR' in su or 'HLG' in su: return 'HDR / HLG'
    if 'SDR' in su: return 'SDR'
    return '其他 / 未识别'


def _dim_bitdepth(s):
    su = s.upper()
    if re.search(r'12[ ._-]?BIT', su): return '12bit'
    if re.search(r'10[ ._-]?BIT', su): return '10bit'
    if re.search(r'8[ ._-]?BIT', su): return '8bit'
    return '其他 / 未识别'


def _dim_audio(s):
    su = s.upper().replace('.', ' ')
    if 'TRUEHD' in su and 'ATMOS' in su: return 'TrueHD Atmos / DTS:X'
    if 'DTS:X' in su or 'DTS-X' in su: return 'TrueHD Atmos / DTS:X'
    if 'TRUEHD' in su or 'DTS-HD' in su: return 'TrueHD / DTS-HD MA'
    if 'LPCM' in su or 'FLAC' in su or 'PCM' in su: return 'LPCM / FLAC'
    if 'AUDIO VIVID' in su or 'AV3A' in su: return 'Audio Vivid / AV3A'
    if 'DDP' in su or 'DD+' in su:
        return 'DD+ Atmos' if 'ATMOS' in su else 'DD+ / E-AC-3'
    if 'E-AC-3' in su or 'EAC3' in su: return 'DD+ / E-AC-3'
    if 'DTS' in su: return 'DTS'
    if 'AC3' in su or 'DD5' in su or 'DOLBY DIGITAL' in su: return 'AC3 / DD'
    if 'AAC' in su: return 'AAC'
    return '其他 / 未识别'


def _dim_fps(s):
    su = s.upper().replace(' ', '')
    if '59.94' in su or '60FPS' in su: return '60 / 59.94 fps'
    if '50FPS' in su: return '50 fps'
    if '29.97' in su or '30FPS' in su: return '30 / 29.97 fps'
    if '25FPS' in su: return '25 fps'
    if '23.976' in su or '24FPS' in su: return '24 / 23.976 fps'
    return '其他 / 未识别'


def _dim_release_group(name):
    stem = name[:-5] if name.lower().endswith('.strm') else name
    m = re.search(r'-([A-Za-z0-9@#&~^]{2,24})$', stem)
    return m.group(1) if m else ''


def recognize_dims(name):
    """从文件名识别各维度档位标签。"""
    return {
        'source': _dim_source(name),
        'resolution': _dim_resolution(name),
        'dolby': _dim_dolby(name),
        'bitdepth': _dim_bitdepth(name),
        'audio': _dim_audio(name),
        'fps': _dim_fps(name),
        'release_group': _dim_release_group(name),
    }


def _tier_rank(label, tiers):
    """label 在 tiers 里的序号；找不到（未识别/用户改了档位名）→ 最后一档。"""
    try:
        return tiers.index(label)
    except ValueError:
        return max(0, len(tiers) - 1)


def compare_cover(a_name, b_name, cover):
    """按覆盖策略比较两个版本：返回 1（a 更优）/ -1（b 更优）/ 0（平）。

    按启用的规则顺序逐维比较：两版本在该维度的档位序号不同则高者胜；
    相同或均未识别则进入下一维度。filesize 维度在 STRM 模式下恒为平（跳过）。
    """
    da = recognize_dims(a_name)
    db = recognize_dims(b_name)
    for r in (cover.get('rules') or []):
        if r.get('enabled') is False:
            continue
        key = r.get('key')
        if key == 'filesize':
            continue
        tiers = r.get('tiers') or COVER_TIERS_DEFAULT.get(key) or []
        if not tiers:
            continue
        if key == 'release_group':
            groups = r.get('groups') or []
            ga = da.get('release_group') or ''
            gb = db.get('release_group') or ''
            ia = groups.index(ga) if ga in groups else len(groups)
            ib = groups.index(gb) if gb in groups else len(groups)
        else:
            ia = _tier_rank(da.get(key) or '', tiers)
            ib = _tier_rank(db.get(key) or '', tiers)
        if ia != ib:
            return 1 if ia < ib else -1
    return 0
