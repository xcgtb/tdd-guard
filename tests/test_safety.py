# -*- coding: utf-8 -*-
"""
删除安全边界测试：
  - 执行计划只删「用户确认过」且「重扫后仍要删」的文件
  - 执行中途异常时计划落到 failed，不停在 executing
  - 附属文件按 stem + 分隔符匹配，E1 不牵连 E10
  - 按 tmdb 删电影：编号完全相等、不重复计数、不碰同号剧集
  - 洗版残留清理与双库清理共用跨入口文件锁
"""
import fcntl
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix='ttdguard_test_safety_'))
os.environ['L_ROOT'] = str(_TMP / 'local')
os.environ['S_ROOT'] = str(_TMP / 'share')
os.environ['CLOUD_L_ROOT'] = str(_TMP / 'cloud')
os.environ['AGENT_DATA'] = str(_TMP / 'data')
os.environ['TMDB_KEY'] = ''
os.environ['TG_BOT_TOKEN'] = ''
os.environ['INGEST_QUIET_MINUTES'] = '0'

sys.path.insert(0, str(Path(__file__).parent.parent / 'app'))
import engine  # noqa: E402


def _reset():
    # engine 可能已被别的测试文件先导入：一律以 engine 实际使用的目录为准
    for root in (engine.L_ROOT, engine.S_ROOT, engine.CLOUD_L_ROOT):
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True, exist_ok=True)
    engine._invalidate_lib_cache()
    engine._strategy = lambda: {
        'decision': 'quality_first', 'multi_season_protect': 'compare',
        'tie_keep_local': False, 'exempt_keywords': [], 'special_action': 'compare',
    }


def _touch(root, rel, text='http://127.0.0.1/fake'):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding='utf-8')
    return p


class _Args:
    def __init__(self, plan='', dry_run=False):
        self.plan = plan; self.dry_run = dry_run; self.notify = False


class TestPlanFilesAreConfirmed:
    def test_file_appearing_after_scan_is_not_deleted(self):
        """洗版进来一个更高版本后，原来的「最优版」变成低版本——它不在用户确认过的计划里，不能被删"""
        _reset()
        d = '电影/华语电影/多版本电影 (2020)'
        v720 = _touch(engine.S_ROOT, f'{d}/多版本电影.720p.strm')
        v1080 = _touch(engine.S_ROOT, f'{d}/多版本电影.1080p.strm')
        pid = engine.save_plan(engine.build_plan())
        plan = engine.load_plan(pid)
        assert [a['files'] for a in plan['actions']] == [[str(v720)]]

        v2160 = _touch(engine.S_ROOT, f'{d}/多版本电影.2160p.strm')
        engine._invalidate_lib_cache()
        res = engine.action_inter_clean(_Args(plan=pid))

        assert res['status'] == 'success', res
        assert res['unconfirmed_files'] == 1
        assert not v720.exists()
        assert v1080.exists(), '扫描后才变成低版本的文件不在计划里，必须保留'
        assert v2160.exists()
        assert engine.load_plan(pid)['state'] == 'done'

    def test_action_whose_files_all_changed_is_skipped(self):
        _reset()
        d = '电影/华语电影/换片电影 (2021)'
        v720 = _touch(engine.S_ROOT, f'{d}/换片电影.720p.strm')
        _touch(engine.S_ROOT, f'{d}/换片电影.1080p.strm')
        pid = engine.save_plan(engine.build_plan())
        v720.unlink()
        _touch(engine.S_ROOT, f'{d}/换片电影.480p.strm')
        engine._invalidate_lib_cache()
        res = engine.action_inter_clean(_Args(plan=pid))
        assert res['status'] == 'success'
        assert res['skipped'] == 1
        assert (engine.S_ROOT / d / '换片电影.480p.strm').exists()

    def test_exception_marks_plan_failed(self):
        _reset()
        d = '电影/华语电影/异常电影 (2022)'
        _touch(engine.S_ROOT, f'{d}/异常电影.720p.strm')
        _touch(engine.S_ROOT, f'{d}/异常电影.1080p.strm')
        pid = engine.save_plan(engine.build_plan())
        orig = engine.build_plan

        def boom():
            raise OSError('挂载掉了')
        engine.build_plan = boom
        try:
            engine.action_inter_clean(_Args(plan=pid))
            assert False, '异常应该继续抛出，由任务系统记录'
        except OSError:
            pass
        finally:
            engine.build_plan = orig
        assert engine.load_plan(pid)['state'] == 'failed'


class TestSidecar:
    def test_prefix_boundary(self):
        assert engine._is_sidecar_of('A.S01E01', 'A.S01E01')
        assert engine._is_sidecar_of('A.S01E01', 'A.S01E01-mediainfo')
        assert engine._is_sidecar_of('A.S01E01', 'A.S01E01.zh')
        assert not engine._is_sidecar_of('A - S01E1', 'A - S01E10')
        assert not engine._is_sidecar_of('A - S01E1', 'A - S01E12.zh')

    def test_deleting_e1_keeps_e10_sidecars(self):
        _reset()
        d = '剧集/国产剧集/边界剧 (2020)/Season 01'
        e1 = _touch(engine.S_ROOT, f'{d}/边界剧 - S01E1.strm')
        _touch(engine.S_ROOT, f'{d}/边界剧 - S01E1.nfo')
        _touch(engine.S_ROOT, f'{d}/边界剧 - S01E1.zh.srt')
        _touch(engine.S_ROOT, f'{d}/边界剧 - S01E10.strm')
        _touch(engine.S_ROOT, f'{d}/边界剧 - S01E10.nfo')
        _touch(engine.S_ROOT, f'{d}/边界剧 - S01E10.zh.srt')
        r = engine.safe_delete_files([e1], engine.S_ROOT, None, dry_run=False)
        assert r['strm_removed'] == 1
        left = sorted(p.name for p in (engine.S_ROOT / d).iterdir())
        assert left == ['边界剧 - S01E10.nfo', '边界剧 - S01E10.strm', '边界剧 - S01E10.zh.srt']


class TestMovieByTmdb:
    def test_exact_id_nested_and_series_excluded(self):
        _reset()
        want = _touch(engine.L_ROOT, '电影/外语电影/片A {tmdb-123}/片A.1080p.strm')
        nested = _touch(engine.L_ROOT, '电影/外语电影/片A {tmdb-123}/花絮 {tmdb-123}/花絮.strm')
        under = _touch(engine.L_ROOT, '电影/外语电影/片C {tmdb_123}/片C.strm')
        _touch(engine.L_ROOT, '电影/外语电影/片B {tmdb-1234}/片B.1080p.strm')
        _touch(engine.L_ROOT, '剧集/欧美剧集/同号剧 {tmdb-123}/Season 01/同号剧.S01E01.strm')
        got = sorted(engine.find_movie_strms_by_tmdb(engine.L_ROOT, '123'))
        assert got == sorted([want, nested, under])

    def test_non_numeric_id_matches_nothing(self):
        _reset()
        _touch(engine.L_ROOT, '电影/外语电影/片A {tmdb-123}/片A.strm')
        assert engine.find_movie_strms_by_tmdb(engine.L_ROOT, '') == []
        assert engine.find_movie_strms_by_tmdb(engine.L_ROOT, '12a') == []


class TestOrphanCleanLock:
    def test_real_clean_is_busy_while_lock_held(self):
        _reset()
        d = _touch(engine.L_ROOT, '电影/华语电影/残留 (2020)/残留.nfo').parent
        engine.DATA_DIR.mkdir(parents=True, exist_ok=True)
        holder = open(engine.LOCK_FILE, 'w')
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            assert engine.clean_orphan_dirs([str(d)], dry_run=True)['count'] == 1
            res = engine.clean_orphan_dirs([str(d)], dry_run=False)
            assert res['status'] == 'busy'
            assert d.exists()
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()
        res = engine.clean_orphan_dirs([str(d)], dry_run=False)
        assert res['count'] == 1 and not d.exists()


class TestReviewFollowups:
    def test_series_layouts_are_not_deleted_as_movies(self):
        _reset()
        # 季目录 + 纯数字集名：文件名看不出是剧集
        _touch(engine.L_ROOT, '电视剧/某剧 (2020) {tmdb-500}/Season 1/01.strm')
        # 剧集分类下的扁平目录
        _touch(engine.L_ROOT, '剧集/国产剧集/另一剧 {tmdb-500}/另一剧 - 01.strm')
        # 真正的电影，文件名里带 Ep —— 不能被当成剧集而删不掉
        ep4 = _touch(engine.L_ROOT, '电影/外语电影/星球大战 Ep 4 {tmdb-500}/Star Wars Ep 4.1080p.strm')
        assert engine.find_movie_strms_by_tmdb(engine.L_ROOT, '500') == [ep4]

    def test_deleting_one_version_keeps_other_versions_sidecars(self):
        _reset()
        d = '电影/外语电影/Movie (2020)'
        low = _touch(engine.S_ROOT, f'{d}/Movie (2020).strm')
        _touch(engine.S_ROOT, f'{d}/Movie (2020).nfo')
        _touch(engine.S_ROOT, f'{d}/Movie (2020)-mediainfo.json')
        _touch(engine.S_ROOT, f'{d}/Movie (2020) - 2160p.strm')
        _touch(engine.S_ROOT, f'{d}/Movie (2020) - 2160p.nfo')
        _touch(engine.S_ROOT, f'{d}/Movie (2020) - 2160p-mediainfo.json')
        engine.safe_delete_files([low], engine.S_ROOT, None, dry_run=False)
        left = sorted(p.name for p in (engine.S_ROOT / d).iterdir())
        assert left == ['Movie (2020) - 2160p-mediainfo.json', 'Movie (2020) - 2160p.nfo',
                        'Movie (2020) - 2160p.strm']
