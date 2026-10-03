# -*- coding: utf-8 -*-
"""运行时共享状态（叶子模块）：路径常量、运行时配置、跨模块可变缓存与锁、日志与时区。

只依赖标准库与 config；其它业务模块一律 `from . import state` 后在调用时取 `state.X`，
这样 reload_config() 重新绑定 RUNTIME_CFG / EMBY_* 、测试改写路径常量都能即时生效。
"""
import os, re, sys, time, threading, logging
from pathlib import Path

from . import config as _cfg


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
# state/ 下的运行状态全部存 SQLite（STATE_DIR/ttd-guard.db，见 storage.py）；旧 JSON 文件位置见 storage.LEGACY_*。
# 追更状态在 sub_state 表里的 store 键（测试可改成别的键做隔离）
SUB_STORE = 'subscriptions_state.json'

# ═══════════════ 目录生命周期 / 孤儿治理（白皮书 §15+§16）═══════════════
_CATEGORY_NAMES = {
    '电影', '剧集', '儿童节目', '综艺', '动漫', '纪录片', '演唱会',
    '国产电影', '华语电影', '欧美电影', '日韩电影', '动画电影', '外语电影', '其他电影',
    '国产剧集', '欧美剧集', '日韩剧集', '其他剧集',
    '国产剧', '欧美剧', '日韩剧',
}
PLAN_TTL = 2 * 3600

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


# ═══════════════════ Lib 短时效缓存 ═══════════════════
# 作用：短时间内多个只读入口（白名单命中预览等）连续调用 build_plan/_get_lib 时，
# 30 秒内复用同一份双库遍历结果，大库上省几秒到几十秒。
# 注意：「扫描双库」完成后和「清理成功」后都会主动失效缓存——
# 执行清理前的二次校验必须基于最新磁盘状态，不能吃扫描时的快照。


_ep_cache = {'ts': 0, 'data': None}
_WASH_RESIDUAL_LOCK = threading.Lock()
_ep_lock = threading.Lock()


# ═══════════════════ 入库静默期 ═══════════════════
# 剧集/电影刚入库时往往只入了一部分（分享转存、STRM 还在陆续生成），
# 此时对比双库会得到"分享仅 3 集、本地 6 集 → 淘汰分享"这类偏差结论。
# 规则：一个标题（含双库、所有季）的任一目录在 ingest_quiet_minutes 分钟内有新增/变动，
# 就整体不进入治理队列（静默跳过，不出现在清单里）；过了静默期再自然纳入。
# 判定用目录 mtime（每个季目录只 stat 一次，几千次即可覆盖整库），不逐文件 stat。
_QUIET_LAST = {'n': 0}


_emby_index_cache = {'ts': 0, 'data': None}
_emby_index_refresh_lock = threading.Lock()


_emby_lib_cache = {'ts': 0, 'data': None}


_overview_refresh_lock = threading.Lock()


# ═══════════════ 探索页实时集数（绕过统一快照）═══════════════
# 背景：探索页此前依赖 library_snapshot.json（30 分钟强制缓存），快照本身又建立在
# emby_library_overview 的产物之上，而那个磁盘缓存只在「片库映射」全量对照时才写。
# 三者新鲜度互不相同，导致刚入库的集在探索页迟迟不更新，且刷新页面永远不自愈。
# 这里的实时查询只问「这一部剧现在有哪些集」，1 次 Series + 1~2 次 Episode，
# 单剧毫秒级；配合并发 + 短 TTL 微缓存，既跟得上入库又能扛住连点刷新。
_live_eps_cache = {}
_live_eps_lock = threading.Lock()


_tmdb_scan_progress = {
    'running': False, 'total': 0, 'done': 0, 'stage': '',
    'started_at': 0, 'finished_at': 0, 'stats': {}, 'error': '',
}


_tmdb_scan_lock = threading.Lock()


# ============ 性能缓存（5 分钟 TTL）============
_strm_count_cache = {'ts': 0, 'local': 0, 'share': 0}
_lib_stats_cache = {'ts': 0, 'data': None}
_CACHE_TTL = 300


_strm_count_refreshing = threading.Lock()
