# -*- coding: utf-8 -*-
"""
engine.py 治理逻辑测试（build_plan / action_inter_clean）。

覆盖白皮书第22节里点名、但当前完全没有测试保护的场景，
本轮先补：
  - 电影白名单豁免（此前是 bug：白名单只在电视剧分支生效，电影完全不检查）
  - 电影画质对比基线（确认修复白名单没有连带破坏正常删除逻辑）
  - 清理任务的跨入口互斥锁（action_inter_clean 内部的 flock）
  - 分享独有 Season 默认保护（此前是 bug：本地没有的季，只要分享库这边缺集/
    错集就会被判定为 gap 直接删掉分享 STRM——两边都没有的资源就再也找不回来了）

运行方式（本地没有 pytest 时，用 tests/run_engine_tests.py 里的极简 runner；
CI / 有 pytest 的环境直接 `pytest tests/ -v` 即可，写法兼容两者）。
"""
import fcntl
import os
import shutil
import sys
import tempfile
from pathlib import Path

# 在 import engine 之前把三个媒体根目录和数据目录都指到临时目录，
# 避免测试误碰到真实 NAS 上的 /media /data。
_TMP = Path(tempfile.mkdtemp(prefix='ttdguard_test_'))
os.environ['L_ROOT'] = str(_TMP / 'local')
os.environ['S_ROOT'] = str(_TMP / 'share')
os.environ['CLOUD_L_ROOT'] = str(_TMP / 'cloud')
os.environ['AGENT_DATA'] = str(_TMP / 'data')
os.environ['TMDB_KEY'] = ''
os.environ['TG_BOT_TOKEN'] = ''

sys.path.insert(0, str(Path(__file__).parent.parent / 'app'))
import engine  # noqa: E402


def _strm(root: Path, folder: str, filename: str):
    """在 root/folder 下建一个空 .strm 文件（内容对治理逻辑无所谓，只看路径和文件名）"""
    d = root / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_text('http://127.0.0.1:12366/fake', encoding='utf-8')


def _reset_libs():
    """清掉两库目录内容 + 失效 Lib 缓存，保证每个测试互不干扰"""
    for root in (engine.L_ROOT, engine.S_ROOT, engine.CLOUD_L_ROOT):
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True, exist_ok=True)
    engine._invalidate_lib_cache()


def _patch_strategy(monkeypatch_dict):
    """覆盖 engine._strategy()，避免测试依赖 /data/config.json 的真实内容"""
    base = {
        'decision': 'quality_first',
        'multi_season_protect': True,
        'tie_keep_local': False,
        'exempt_keywords': [],
        'special_action': 'compare',
    }
    base.update(monkeypatch_dict)
    engine._strategy = lambda: base


def _acts_for_title(acts, title_substr):
    return [a for a in acts if title_substr in a.meta.get('title', '')]


# ═══════════════════ 电影白名单 ═══════════════════
class TestMovieWhitelist:
    def test_whitelisted_movie_is_not_touched(self):
        """
        回归测试：修复前 build_plan() 的电影分支完全不检查白名单，
        命中白名单关键词的电影仍然会被正常按画质比较、生成删除 Action。
        本测试锁定：白名单命中后必须只产生一个 kind='exempt' 的 Act，
        不能出现 kind in ('loc', 'shr')。
        """
        _reset_libs()
        _patch_strategy({'exempt_keywords': ['白名单测试电影']})

        _strm(engine.L_ROOT, '白名单测试电影 (2020)', '白名单测试电影.2160p.strm')
        _strm(engine.S_ROOT, '白名单测试电影 (2020)', '白名单测试电影.720p.strm')

        acts = engine.build_plan()
        hits = _acts_for_title(acts, '白名单测试电影')

        assert len(hits) == 1
        assert hits[0].kind == 'exempt'
        assert not any(a.kind in ('loc', 'shr') for a in hits)

    def test_non_whitelisted_movie_still_compares_normally(self):
        """确认白名单修复没有误伤：没命中关键词的电影还是按画质正常比较"""
        _reset_libs()
        _patch_strategy({'exempt_keywords': ['这个关键词不会命中任何东西']})

        _strm(engine.L_ROOT, '画质对比电影 (2021)', '画质对比电影.1080p.strm')
        _strm(engine.S_ROOT, '画质对比电影 (2021)', '画质对比电影.2160p.strm')

        acts = engine.build_plan()
        hits = _acts_for_title(acts, '画质对比电影')

        assert len(hits) == 1
        # 分享 2160p > 本地 1080p，按默认策略应删本地、保留分享
        assert hits[0].kind == 'loc'
        assert hits[0].meta['reason'] == 'share_better'

    def test_tie_break_defaults_to_deleting_local(self):
        """画质相同时，默认 tie_keep_local=False，平局删本地保留分享"""
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})

        _strm(engine.L_ROOT, '平局电影 (2022)', '平局电影.1080p.strm')
        _strm(engine.S_ROOT, '平局电影 (2022)', '平局电影.1080p.strm')

        acts = engine.build_plan()
        hits = _acts_for_title(acts, '平局电影')

        assert len(hits) == 1
        assert hits[0].kind == 'loc'


# ═══════════════════ 跨入口并发锁 ═══════════════════
class TestCleanupLock:
    def test_dry_run_does_not_take_the_lock(self):
        """dry-run 只读不写，不应该受锁影响，也不应该去抢锁"""
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})

        class Args:
            plan = ''
            dry_run = True

        # 手动占住锁，确认 dry-run 完全不受影响
        engine.DATA_DIR.mkdir(parents=True, exist_ok=True)
        holder = open(engine.LOCK_FILE, 'w')
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            res = engine.action_inter_clean(Args())
            assert res['status'] == 'success'
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()

    def test_second_real_cleanup_gets_busy_while_first_holds_lock(self):
        """
        模拟三个入口（CLI/Web/Bot）里任意两个同时发起真正的清理：
        第二个必须拿不到锁，返回 status=busy，而不是也去跑一遍 safe_delete_files。
        """
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})

        class Args:
            plan = ''
            dry_run = False

        engine.DATA_DIR.mkdir(parents=True, exist_ok=True)
        holder = open(engine.LOCK_FILE, 'w')
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            res = engine.action_inter_clean(Args())
            assert res['status'] == 'busy'
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()

    def test_lock_is_released_after_cleanup_so_next_call_succeeds(self):
        """锁必须在清理结束后释放，不能一直占着导致后续任务永远 busy"""
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})

        class Args:
            plan = ''
            dry_run = False

        res1 = engine.action_inter_clean(Args())
        assert res1['status'] == 'success'
        res2 = engine.action_inter_clean(Args())
        assert res2['status'] == 'success'


# ═══════════════════ 分享独有 Season 默认保护 ═══════════════════
class TestShareOnlySeasonProtection:
    def test_complete_share_only_season_is_protected(self):
        """本地完全没有这部剧，分享库这季集数是完整的——不该有任何删除动作"""
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})

        for ep in (1, 2, 3):
            _strm(engine.S_ROOT, '分享独有剧集A (2020)/Season 01', f'E{ep:02d}.strm')

        acts = engine.build_plan()
        hits = _acts_for_title(acts, '分享独有剧集A')

        assert len(hits) == 1
        assert hits[0].kind == 'keep'
        assert not any(a.kind in ('loc', 'shr') for a in hits)

    def test_incomplete_share_only_season_is_protected_not_deleted(self):
        """
        回归测试：修复前，本地没有的 Season 只要分享库这边缺集/错集，
        就会被 _analyze() 判定为 gap，直接生成 kind='shr' 的删除 Action——
        这季在两个库里唯一的资源就这样被删掉了，以后想找都找不回来。
        修复后必须是 kind='keep'，只报告不删除。
        """
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})

        for ep in (1, 3):  # 缺 E02，中间断层
            _strm(engine.S_ROOT, '分享独有剧集B (2021)/Season 01', f'E{ep:02d}.strm')

        acts = engine.build_plan()
        hits = _acts_for_title(acts, '分享独有剧集B')

        assert len(hits) == 1
        assert hits[0].kind == 'keep'
        assert hits[0].meta['reason'] == 'share_only_gap'
        assert not any(a.kind in ('loc', 'shr') for a in hits)

    def test_share_only_season_never_shows_up_as_deletable_in_check(self):
        """action_inter_check() 返回给前端的字段里，分享独有的季不该出现在
        del_share_items（会被展示成"待清理分享"）里，只能出现在 protected_items 里"""
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})

        for ep in (1, 3):
            _strm(engine.S_ROOT, '分享独有剧集C (2022)/Season 01', f'E{ep:02d}.strm')

        class Args:
            kw = ''

        res = engine.action_inter_check(Args())
        share_titles = [i['title'] for i in res['del_share_items']]
        protected_titles = [i['title'] for i in res['protected_items']]

        assert not any('分享独有剧集C' in t for t in share_titles)
        assert any('分享独有剧集C' in t for t in protected_titles)


# ═══════════════════ 电视剧逐集比较（白皮书 §7）═══════════════════
class TestSeasonReplaceable:
    """白皮书 §7：分享 Season 必须逐集覆盖本地 + 逐集质量 >= 本地"""

    def test_whitepaper_positive_example(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _strm(engine.L_ROOT, '逐集正例 (2020)/Season 01', 'E01.2160p.strm')
        _strm(engine.L_ROOT, '逐集正例 (2020)/Season 01', 'E02.1080p.strm')
        _strm(engine.L_ROOT, '逐集正例 (2020)/Season 01', 'E03.1080p.strm')
        _strm(engine.S_ROOT, '逐集正例 (2020)/Season 01', 'E01.2160p.strm')
        _strm(engine.S_ROOT, '逐集正例 (2020)/Season 01', 'E02.1080p.strm')
        _strm(engine.S_ROOT, '逐集正例 (2020)/Season 01', 'E03.2160p.strm')

        acts = engine.build_plan()
        hits = _acts_for_title(acts, '逐集正例')
        assert len(hits) == 1
        assert hits[0].kind == 'loc'
        assert hits[0].meta['reason'] == 'season_all_pass'

    def test_whitepaper_negative_example(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _strm(engine.L_ROOT, '逐集反例 (2020)/Season 01', 'E01.2160p.strm')
        _strm(engine.L_ROOT, '逐集反例 (2020)/Season 01', 'E02.2160p.strm')
        _strm(engine.L_ROOT, '逐集反例 (2020)/Season 01', 'E03.1080p.strm')
        _strm(engine.S_ROOT, '逐集反例 (2020)/Season 01', 'E01.1080p.strm')
        _strm(engine.S_ROOT, '逐集反例 (2020)/Season 01', 'E02.2160p.strm')
        _strm(engine.S_ROOT, '逐集反例 (2020)/Season 01', 'E03.2160p.strm')

        acts = engine.build_plan()
        hits = _acts_for_title(acts, '逐集反例')
        assert len(hits) == 1
        assert hits[0].kind == 'keep'
        assert hits[0].meta['reason'] == 'season_partial_pass'

    def test_share_missing_episode_blocks(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _strm(engine.L_ROOT, '分享缺集 (2020)/Season 01', 'E01.2160p.strm')
        _strm(engine.L_ROOT, '分享缺集 (2020)/Season 01', 'E02.2160p.strm')
        _strm(engine.L_ROOT, '分享缺集 (2020)/Season 01', 'E03.2160p.strm')
        _strm(engine.S_ROOT, '分享缺集 (2020)/Season 01', 'E01.2160p.strm')
        _strm(engine.S_ROOT, '分享缺集 (2020)/Season 01', 'E03.2160p.strm')

        acts = engine.build_plan()
        hits = _acts_for_title(acts, '分享缺集')
        assert len(hits) == 1
        assert hits[0].kind == 'keep'
        # 分享集数(2) < 本地集数(3)，走 l_eps>=s_eps 分支
        assert hits[0].meta['reason'] == 'share_behind'

    def test_share_more_episodes_passes(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _strm(engine.L_ROOT, '分享更多 (2020)/Season 01', 'E01.2160p.strm')
        _strm(engine.L_ROOT, '分享更多 (2020)/Season 01', 'E02.2160p.strm')
        _strm(engine.S_ROOT, '分享更多 (2020)/Season 01', 'E01.2160p.strm')
        _strm(engine.S_ROOT, '分享更多 (2020)/Season 01', 'E02.2160p.strm')
        _strm(engine.S_ROOT, '分享更多 (2020)/Season 01', 'E03.2160p.strm')

        acts = engine.build_plan()
        hits = _acts_for_title(acts, '分享更多')
        assert len(hits) == 1
        assert hits[0].kind == 'loc'
        assert hits[0].meta['reason'] == 'share_more'

    def test_all_tie_2160_pass(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _strm(engine.L_ROOT, '全平局 (2020)/Season 01', 'E01.2160p.strm')
        _strm(engine.L_ROOT, '全平局 (2020)/Season 01', 'E02.2160p.strm')
        _strm(engine.S_ROOT, '全平局 (2020)/Season 01', 'E01.2160p.strm')
        _strm(engine.S_ROOT, '全平局 (2020)/Season 01', 'E02.2160p.strm')

        acts = engine.build_plan()
        hits = _acts_for_title(acts, '全平局')
        assert len(hits) == 1
        assert hits[0].kind == 'loc'
        assert hits[0].meta['reason'] == 'season_all_pass'

    def test_share_only_season_still_protected(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _strm(engine.S_ROOT, '分享独有回归 (2020)/Season 01', 'E01.2160p.strm')
        _strm(engine.S_ROOT, '分享独有回归 (2020)/Season 01', 'E03.2160p.strm')

        acts = engine.build_plan()
        hits = _acts_for_title(acts, '分享独有回归')
        assert len(hits) == 1
        assert hits[0].kind == 'keep'
        assert hits[0].meta['reason'] == 'share_only_gap'

    def test_whitelist_still_wins(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': ['逐集白名单']})
        _strm(engine.L_ROOT, '逐集白名单 (2020)/Season 01', 'E01.2160p.strm')
        _strm(engine.S_ROOT, '逐集白名单 (2020)/Season 01', 'E01.1080p.strm')

        acts = engine.build_plan()
        hits = _acts_for_title(acts, '逐集白名单')
        assert len(hits) == 1
        assert hits[0].kind == 'exempt'


# ═══════════════════ 云端源置信度（白皮书 §13）═══════════════════
class TestCloudVideoConfidence:
    """只有 exact / normalized 允许自动删；其他一律 STRM 保留"""

    def _setup(self, conf, vids=None):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _strm(engine.L_ROOT, '云源测试 (2020)/Season 01', 'E01.1080p.strm')
        f = next(engine.L_ROOT.rglob('*.strm'))
        orig = engine.cloud_videos
        engine.cloud_videos = lambda *a, **k: (vids or [], conf)
        return f, orig

    def _teardown(self, orig):
        engine.cloud_videos = orig

    def test_exact_allows_deletion(self):
        f, orig = self._setup('exact', [])
        try:
            r = engine.safe_delete_files([f], engine.L_ROOT, engine.CLOUD_L_ROOT, dry_run=False)
            assert r['strm_removed'] == 1
            assert not f.exists()
        finally:
            self._teardown(orig)

    def test_normalized_allows_deletion(self):
        f, orig = self._setup('normalized', [])
        try:
            r = engine.safe_delete_files([f], engine.L_ROOT, engine.CLOUD_L_ROOT, dry_run=False)
            assert r['strm_removed'] == 1
            assert not f.exists()
        finally:
            self._teardown(orig)

    def test_missing_blocks_strm(self):
        f, orig = self._setup('none')
        try:
            r = engine.safe_delete_files([f], engine.L_ROOT, engine.CLOUD_L_ROOT, dry_run=False)
            assert r['strm_removed'] == 0
            assert r['cloud_missing'] == 1
            assert f.exists()
        finally:
            self._teardown(orig)

    def test_fallback_blocks_strm(self):
        f, orig = self._setup('unique_fallback')
        try:
            r = engine.safe_delete_files([f], engine.L_ROOT, engine.CLOUD_L_ROOT, dry_run=False)
            assert r['strm_removed'] == 0
            assert r['cloud_fallback'] == 1
            assert f.exists()
        finally:
            self._teardown(orig)

    def test_ambiguous_blocks_strm(self):
        f, orig = self._setup('ambiguous')
        try:
            r = engine.safe_delete_files([f], engine.L_ROOT, engine.CLOUD_L_ROOT, dry_run=False)
            assert r['strm_removed'] == 0
            assert r['cloud_ambiguous'] == 1
            assert f.exists()
        finally:
            self._teardown(orig)


import json as _json
import time as _time


class TestPlanLifecycle:

    def test_action_id_uses_stable_media_key(self):
        act = engine.Act('loc', '📺 《闪电侠 (2014)》S01 (整剧零和：分享全面达标 → 删本地)',
                         'detail', [], {'title': '闪电侠 (2014)', 'season': 1})
        assert act.action_id == 'loc:tv:闪电侠 (2014):S01'
        assert '整剧零和' not in act.action_id

    def test_movie_action_id_has_no_season(self):
        act = engine.Act('shr', '🎬 《测试电影》 (本地更优)',
                         'detail', [], {'title': '测试电影'})
        assert act.action_id == 'shr:movie:测试电影'

    def test_save_plan_writes_schema_v2(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _strm(engine.L_ROOT, '剧A (2020)/Season 01', 'E01.1080p.strm')
        _strm(engine.S_ROOT, '剧A (2020)/Season 01', 'E01.1080p.strm')
        acts = engine.build_plan()
        pid = engine.save_plan(acts)
        assert pid is not None
        pf = engine.STATE_DIR / f'plan_{pid}.json'
        assert pf.exists()
        data = _json.loads(pf.read_text(encoding='utf-8'))
        assert data.get('schema_version') == 2
        assert data.get('state') == 'pending'
        assert isinstance(data.get('actions'), list)
        assert len(data['actions']) >= 1
        first = data['actions'][0]
        assert 'action_id' in first
        assert 'media_key' in first

    def test_load_plan_rejects_old_schema(self):
        engine.STATE_DIR.mkdir(parents=True, exist_ok=True)
        old_pf = engine.STATE_DIR / 'plan_deadbeef.json'
        old_pf.write_text(_json.dumps({
            'id': 'deadbeef', 'ts': _time.time(), 'keys': ['loc|x']
        }), encoding='utf-8')
        try:
            assert engine.load_plan('deadbeef') is None
        finally:
            old_pf.unlink(missing_ok=True)

    def test_expired_plan_rejected(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _strm(engine.L_ROOT, '剧B (2020)/Season 01', 'E01.1080p.strm')
        _strm(engine.S_ROOT, '剧B (2020)/Season 01', 'E01.1080p.strm')
        acts = engine.build_plan()
        pid = engine.save_plan(acts)
        assert pid is not None
        pf = engine.STATE_DIR / f'plan_{pid}.json'
        data = _json.loads(pf.read_text(encoding='utf-8'))
        data['ts'] = _time.time() - (engine.PLAN_TTL + 3600)
        pf.write_text(_json.dumps(data, ensure_ascii=False), encoding='utf-8')

        class A: pass
        a = A(); a.plan = pid; a.dry_run = False
        res = engine.action_inter_clean(a)
        assert res.get('status') == 'error'
        assert res.get('code') == 'plan_expired'

    def test_done_plan_cannot_be_reexecuted(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _strm(engine.L_ROOT, '剧C (2020)/Season 01', 'E01.1080p.strm')
        _strm(engine.S_ROOT, '剧C (2020)/Season 01', 'E01.1080p.strm')
        acts = engine.build_plan()
        pid = engine.save_plan(acts)
        assert pid is not None
        engine.save_plan_state(pid, 'done')

        class A: pass
        a = A(); a.plan = pid; a.dry_run = False
        res = engine.action_inter_clean(a)
        assert res.get('status') == 'error'
        assert res.get('code') == 'plan_used'



def _aux_file(root, folder, filename, content='x'):
    d = root / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_text(content, encoding='utf-8')


class TestDirLifecycle:

    def test_movie_dir_cleaned_after_strm_delete(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _strm(engine.L_ROOT, '电影/华语电影/测试电影 (2020)', '测试电影.1080p.strm')
        f = next(engine.L_ROOT.rglob('*.strm'))
        r = engine.safe_delete_files([f], engine.L_ROOT, None, dry_run=False)
        assert r['strm_removed'] == 1
        assert not (engine.L_ROOT / '电影/华语电影/测试电影 (2020)').exists()
        assert (engine.L_ROOT / '电影/华语电影').is_dir()
        assert (engine.L_ROOT / '电影').is_dir()

    def test_category_dir_never_deleted(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _strm(engine.L_ROOT, '电影', 'x.strm')
        f = next(engine.L_ROOT.rglob('*.strm'))
        engine.safe_delete_files([f], engine.L_ROOT, None, dry_run=False)
        assert (engine.L_ROOT / '电影').is_dir()

    def test_unknown_file_blocks_dir_delete(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _strm(engine.L_ROOT, '电影/华语电影/测试电影 (2020)', '测试电影.1080p.strm')
        _aux_file(engine.L_ROOT, '电影/华语电影/测试电影 (2020)', 'readme.log', 'x')
        f = next(engine.L_ROOT.rglob('*.strm'))
        engine.safe_delete_files([f], engine.L_ROOT, None, dry_run=False)
        assert (engine.L_ROOT / '电影/华语电影/测试电影 (2020)').is_dir()
        assert (engine.L_ROOT / '电影/华语电影/测试电影 (2020)/readme.log').exists()

    def test_metadata_deleted_with_media(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _strm(engine.L_ROOT, '电影/华语电影/测试电影 (2020)', '测试电影.1080p.strm')
        _aux_file(engine.L_ROOT, '电影/华语电影/测试电影 (2020)', 'mediainfo.json', '{}')
        _aux_file(engine.L_ROOT, '电影/华语电影/测试电影 (2020)', 'poster.jpg', 'img')
        f = next(engine.L_ROOT.rglob('*.strm'))
        engine.safe_delete_files([f], engine.L_ROOT, None, dry_run=False)
        assert not (engine.L_ROOT / '电影/华语电影/测试电影 (2020)').exists()

    def test_series_root_cleaned_after_all_seasons(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        for sn in ('Season 01', 'Season 02'):
            _strm(engine.L_ROOT, '剧集/欧美剧集/测试剧 (2020)/' + sn, 'E01.1080p.strm')
        files = list(engine.L_ROOT.rglob('*.strm'))
        engine.safe_delete_files(files, engine.L_ROOT, None, dry_run=False)
        assert not (engine.L_ROOT / '剧集/欧美剧集/测试剧 (2020)').exists()
        assert (engine.L_ROOT / '剧集/欧美剧集').is_dir()

    def test_scan_orphans_finds_unknown_files(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _aux_file(engine.L_ROOT, '电影/华语电影', 'weird.xyz', 'x')
        _aux_file(engine.L_ROOT, '电影/华语电影/电影A (2020)', 'mediainfo.json', '{}')
        _strm(engine.L_ROOT, '电影/华语电影/电影B (2020)', 'x.strm')
        items = engine.scan_orphans(max_depth=3)
        paths = [it['path'] for it in items]
        assert any('weird.xyz' in p for p in paths)
        assert not any('mediainfo.json' in p for p in paths)
        assert not any(p.endswith('.strm') for p in paths)



class TestOrphanPerf:

    def test_scan_orphans_respects_max_depth(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        # 3 层结构
        _aux_file(engine.L_ROOT, '电影/华语电影/剧A (2020)', 'x.xyz', 'x')
        # 5 层结构
        _aux_file(engine.L_ROOT, '电影/华语电影/剧B (2020)/Season 01/extra', 'y.xyz', 'y')
        # max_depth=3 应该只找到 3 层里的
        items = engine.scan_orphans(max_depth=3)
        paths = [it['path'] for it in items]
        assert any('x.xyz' in p for p in paths)
        assert not any('y.xyz' in p for p in paths)

    def test_scan_orphans_ignores_metadata(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _aux_file(engine.L_ROOT, '电影/华语电影/电影A (2020)', 'mediainfo.json', '{}')
        _aux_file(engine.L_ROOT, '电影/华语电影/电影A (2020)', 'poster.jpg', 'img')
        _aux_file(engine.L_ROOT, '电影/华语电影/电影A (2020)', 'sub.srt', 'srt')
        _aux_file(engine.L_ROOT, '电影/华语电影/电影A (2020)', 'weird.xyz', 'x')
        items = engine.scan_orphans(max_depth=3)
        assert len(items) == 1
        assert items[0]['ext'] == '.xyz'
