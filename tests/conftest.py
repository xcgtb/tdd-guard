# -*- coding: utf-8 -*-
"""测试公共环境：在任何 app 模块导入之前，把媒体根目录 / 数据目录指到同一个会话级临时目录。

- 只有一份模块副本：一律 `from app import ...`，不再把 app/ 塞进 sys.path 做顶层导入；
- 环境变量用 setdefault：外部显式指定时以外部为准（例如在容器里指定 AGENT_DATA 排查问题）；
- isolated_state 夹具：把 state.STATE_DIR 与全部 *_FILE 路径改到 tmp_path，测试结束自动还原。
"""
import os
import sys
import tempfile
from pathlib import Path

try:
    import pytest
except ImportError:          # tests/run_engine_tests.py 的无 pytest 场景：只需要上面的环境变量
    pytest = None

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_SESSION_TMP = Path(tempfile.mkdtemp(prefix='ttdguard_test_'))
os.environ.setdefault('L_ROOT', str(_SESSION_TMP / 'local'))
os.environ.setdefault('S_ROOT', str(_SESSION_TMP / 'share'))
os.environ.setdefault('CLOUD_L_ROOT', str(_SESSION_TMP / 'cloud'))
os.environ.setdefault('AGENT_DATA', str(_SESSION_TMP / 'data'))
os.environ.setdefault('TMDB_KEY', '')
os.environ.setdefault('TG_BOT_TOKEN', '')
os.environ.setdefault('TG_ALLOWED_USERS', '')  # 白名单从 config.json 读，不从这个环境变量的默认值读
# 测试里的 STRM 都是刚创建的，先关掉「入库静默期」，静默期本身在 TestIngestQuietPeriod 里单独测
os.environ.setdefault('INGEST_QUIET_MINUTES', '0')

# state 里所有落在 STATE_DIR 下的文件路径（文件名与 app/state.py 保持一致）
_STATE_FILES = (
    'SUB_STATE_FILE', 'EMBY_LIB_CACHE_FILE', 'INGEST_CACHE_FILE', 'WASH_RESIDUAL_FILE',
    'LIBRARY_SNAPSHOT_FILE', 'GOV_LATEST_FILE', '_EMBY_INDEX_CACHE_FILE',
    '_EMBY_OVERVIEW_CACHE_FILE', 'MANUAL_DONE_FILE', '_STRM_COUNT_CACHE_FILE',
)


def isolated_state(tmp_path, monkeypatch):
    """把 state.STATE_DIR 及其下的全部状态文件改到 tmp_path（文件名不变），返回新的 STATE_DIR。"""
    from app import state
    sd = tmp_path / 'state'
    sd.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(state, 'STATE_DIR', sd)
    for name in _STATE_FILES:
        monkeypatch.setattr(state, name, sd / getattr(state, name).name)
    monkeypatch.setattr(state, 'LOCK_FILE', tmp_path / state.LOCK_FILE.name)
    return sd


if pytest is not None:
    isolated_state = pytest.fixture(isolated_state)
