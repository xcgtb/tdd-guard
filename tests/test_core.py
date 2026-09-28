# -*- coding: utf-8 -*-
"""
core.py 单元测试
运行方式（容器内或本地都行，core.py 不依赖任何 IO/环境变量）：
    pip install pytest
    cd media-agent && pytest tests/ -v
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / 'app'))
import core  # noqa: E402


# ═══════════════════ parse_season_dir ═══════════════════
class TestParseSeasonDir:
    def test_season_word(self):
        assert core.parse_season_dir('Season 1') == 1
        assert core.parse_season_dir('Season10') == 10  # 无分隔符也要认

    def test_s_prefix(self):
        assert core.parse_season_dir('S02') == 2
        assert core.parse_season_dir('s2') == 2

    def test_chinese(self):
        assert core.parse_season_dir('第3季') == 3
        assert core.parse_season_dir('第 12 季') == 12

    def test_specials(self):
        assert core.parse_season_dir('Specials') == 0
        assert core.parse_season_dir('特别篇') == 0
        assert core.parse_season_dir('特典') == 0

    def test_not_a_season_dir(self):
        assert core.parse_season_dir('随便一个文件夹') is None
        assert core.parse_season_dir('') is None

    def test_no_false_positive_on_longer_word(self):
        # S01xxx 不应该被当成 S01（后面紧跟字母数字，负向先行断言要生效）
        assert core.parse_season_dir('S01abc') is None


# ═══════════════════ get_ep ═══════════════════
class TestGetEp:
    def test_sxxexx(self):
        assert core.get_ep('剧名.S01E05.mkv') == (1, 5)
        assert core.get_ep('剧名.S1E5.mkv') == (1, 5)
        assert core.get_ep('剧名.s01ep05.mkv') == (1, 5)

    def test_chinese_episode_with_season_dir(self):
        assert core.get_ep('第05集.mkv', 'Season 2') == (2, 5)

    def test_specials_dir(self):
        assert core.get_ep('SP02.mkv', 'Specials') == (0, 2)
        assert core.get_ep('第3话.mkv', '特别篇') == (0, 3)

    def test_sp_in_filename_without_specials_dir(self):
        # 文件名自带 SP 标记时，即使没有 Specials 目录也应识别为第 0 季
        assert core.get_ep('剧名.特别篇.SP01.mkv', '') == (0, 1)

    def test_no_season_dir_defaults_to_season_1(self):
        assert core.get_ep('EP03.mkv', '') == (1, 3)

    def test_season_dir_but_no_episode_number(self):
        assert core.get_ep('剧名.mkv', 'Season 2') is None

    def test_no_match_at_all(self):
        assert core.get_ep('随便.mkv', '') is None

    def test_no_false_positive_from_resolution(self):
        # 2160p / 1080p 不应被误判为集数
        assert core.get_ep('电影.2160p.mkv', '') is None


# ═══════════════════ title_key ═══════════════════
class TestTitleKey:
    def test_with_tmdb_and_year(self):
        key, disp, base, year = core.title_key('剧名 (2023) {tmdb-12345}')
        assert key == 'tmdb:12345'
        assert base == '剧名'
        assert year == '2023'
        assert disp == '剧名 (2023)'

    def test_without_tmdb(self):
        key, disp, base, year = core.title_key('剧名 [国语]')
        assert key == '剧名'  # 无 tmdb 时 key 退化为展示名
        assert base == '剧名'
        assert year is None

    def test_same_base_different_tmdb_get_different_keys(self):
        k1, *_ = core.title_key('重名剧 (2020) {tmdb-1}')
        k2, *_ = core.title_key('重名剧 (2020) {tmdb-2}')
        assert k1 != k2


# ═══════════════════ get_score / best_score ═══════════════════
class TestScore:
    def test_dv_outranks_everything(self):
        assert core.get_score('xxx.2160p.DV.mkv') > core.get_score('xxx.2160p.REMUX.60FPS.mkv')

    def test_resolution_tiers(self):
        s4k = core.get_score('xxx.2160p.mkv')
        s1080 = core.get_score('xxx.1080p.mkv')
        s720 = core.get_score('xxx.720p.mkv')
        s_none = core.get_score('xxx.mkv')
        assert s4k > s1080 > s720 > s_none == 0

    def test_dovi_alias(self):
        assert core.get_score('xxx.DOLBY.VISION.mkv') >= 10000
        assert core.get_score('xxx.DOVI.mkv') >= 10000

    def test_best_score_picks_max(self):
        names = ['a.720p.mkv', 'b.2160p.DV.mkv', 'c.1080p.mkv']
        assert core.best_score(names) == core.get_score('b.2160p.DV.mkv')

    def test_best_score_empty(self):
        assert core.best_score([]) == 0


# ═══════════════════ share_wins ═══════════════════
class TestShareWins:
    def test_share_strictly_better(self):
        assert core.share_wins(200, 100) is True
        assert core.share_wins(200, 100, tie_keep_local=True) is True

    def test_local_strictly_better(self):
        assert core.share_wins(100, 200) is False
        assert core.share_wins(100, 200, tie_keep_local=True) is False

    def test_tie_default_share_wins(self):
        assert core.share_wins(100, 100) is True

    def test_tie_keep_local_flag(self):
        assert core.share_wins(100, 100, tie_keep_local=True) is False


# ═══════════════════ is_exempt ═══════════════════
class TestExempt:
    def test_default_keyword(self):
        assert core.is_exempt('百家讲坛.S10E01') is True

    def test_not_exempt(self):
        assert core.is_exempt('随便一部剧.S01E01') is False

    def test_custom_keywords(self):
        assert core.is_exempt('我的节目', keywords=['我的']) is True
        assert core.is_exempt('我的节目', keywords=['别的']) is False


# ═══════════════════ fmt_nums ═══════════════════
class TestFmtNums:
    def test_mixed_ranges(self):
        assert core.fmt_nums([1, 2, 3, 5, 6, 9]) == 'E01-E03,E05-E06,E09'

    def test_all_consecutive(self):
        assert core.fmt_nums([1, 2, 3, 4]) == 'E01-E04'

    def test_all_isolated(self):
        assert core.fmt_nums([1, 3, 5]) == 'E01,E03,E05'

    def test_empty(self):
        assert core.fmt_nums([]) == ''

    def test_single(self):
        assert core.fmt_nums([7]) == 'E07'


# ═══════════════════ analyze_season_episodes / is_seq ═══════════════════
class TestAnalyzeSeasonEpisodes:
    def test_complete(self):
        ok, reason = core.analyze_season_episodes([1, 2, 3])
        assert ok is True and reason == '完整'

    def test_missing_prefix(self):
        ok, reason = core.analyze_season_episodes([2, 3, 4])
        assert ok is False
        assert 'E01' in reason and '缺失前序' in reason

    def test_missing_middle(self):
        ok, reason = core.analyze_season_episodes([1, 2, 4, 5])
        assert ok is False
        assert 'E03' in reason and '中间断层' in reason

    def test_missing_both(self):
        ok, reason = core.analyze_season_episodes([3, 4, 6, 7])
        assert ok is False
        assert '缺失前序' in reason and '中间断层' in reason

    def test_empty_episode_list(self):
        ok, reason = core.analyze_season_episodes([])
        assert ok is False and reason == '空剧集'

    def test_exempt_overrides_gaps(self):
        # 白名单命中时，即使有断层也应判定为“完整”
        ok, reason = core.analyze_season_episodes([1, 5], '百家讲坛')
        assert ok is True and reason == '白名单豁免'

    def test_duplicates_and_unsorted_input(self):
        # 乱序 + 重复集号不应影响判断
        ok, _ = core.analyze_season_episodes([3, 1, 2, 2, 1])
        assert ok is True

    def test_is_seq_matches_analyze(self):
        assert core.is_seq([1, 2, 3]) is True
        assert core.is_seq([2, 3, 4]) is False


# ═══════════════════ parse_emby_library ═══════════════════
class TestParseEmbyLibrary:
    def test_path_category_match_takes_priority(self):
        item = {'Path': '/media/儿童节目/xxx', 'Name': '随便', 'Genres': []}
        result = core.parse_emby_library(item)
        assert result == core.DEFAULT_CATEGORIES[0]

    def test_kids_keyword_in_name(self):
        item = {'Path': '/media/未分类/佩奇的世界', 'Name': '佩奇的世界', 'Genres': []}
        assert core.parse_emby_library(item) == core.DEFAULT_CATEGORIES[0]

    def test_documentary_by_genre(self):
        item = {'Path': '/media/x', 'Name': '未知', 'Genres': ['纪录片']}
        assert core.parse_emby_library(item) == core.DEFAULT_CATEGORIES[5]

    def test_movie_vs_tv_region_split(self):
        item = {'Path': '/media/华语电影/xxx', 'Name': 'xxx', 'Genres': []}
        assert core.parse_emby_library(item, is_movie=True) == core.DEFAULT_CATEGORIES[9]

    def test_default_fallback_tv_is_domestic(self):
        item = {'Path': '/media/未知目录/xxx', 'Name': 'xxx', 'Genres': []}
        assert core.parse_emby_library(item, is_movie=False) == core.DEFAULT_CATEGORIES[6]


# ═══════════════════ esc (Markdown 转义) ═══════════════════
class TestEsc:
    def test_escapes_markdown_special_chars(self):
        assert core.esc('a_b*c`d[e') == r'a\_b\*c\`d\[e'

    def test_non_string_input(self):
        assert core.esc(123) == '123'
