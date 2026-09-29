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

def get_ep(name: str, parent_name: str = '') -> Optional[Tuple[int, int]]:
    """从文件名+父目录推断 (季, 集)"""
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
    return (1, ep) if ep is not None else None

@lru_cache(maxsize=16384)
def title_key(folder: str) -> Tuple[str, str, str, Optional[str]]:
    """返回 (唯一key, 展示名, base, 年份)"""
    t = RE_TMDB.search(folder)
    y = RE_YEAR.search(folder)
    year = y.group(1) if y else None
    base = re.sub(r'\s+', ' ', RE_BRACKET.sub('', folder)).strip() or folder
    disp = base + (f' ({year})' if year else '')
    return (f'tmdb:{t.group(1)}' if t else disp), disp, base, year

# ═══════════════════ 画质评分 ═══════════════════
# 画质分层比较，避免「1080p DV > 4K HDR」这类跨分辨率错判。
# 比较维度优先级（高 → 低）：
#   1. 分辨率：2160p > 1080p > 720p > 其他
#   2. HDR 维度（同分辨率才有意义，作为第二比较位）：DV > HDR10/HDR > SDR > 无
#   3. 编码档次：REMUX > WEB-DL/其他
#   4. 帧率：60fps+ > 其他
#   5. 色深：10bit > 8bit
# 每个维度独立成一位，返回可比较的 tuple；逐位比较保证同级才往后比。

def quality_of(name: str) -> Tuple[int, int, int, int, int]:
    """解析文件名，返回 (分辨率, HDR, 编码, 帧率, 色深) 五元组，逐位越大越优。"""
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


def get_score(name: str) -> Tuple[int, int, int, int, int]:
    """兼容旧调用：返回画质五元组（可比较）。"""
    return quality_of(name)


def best_score(names: Iterable[str]):
    """取一组文件中的最高画质五元组。"""
    return max((quality_of(n) for n in names), default=(0, 0, 0, 0, 0))


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