# -*- coding: utf-8 -*-
"""测试公共环境：在任何 app 模块导入之前，把媒体根目录 / 数据目录指到同一个会话级临时目录。

- 只有一份模块副本：一律 `from app import ...`，不再把 app/ 塞进 sys.path 做顶层导入；
- 环境变量用 setdefault：外部显式指定时以外部为准（例如在容器里指定 AGENT_DATA 排查问题）；
- isolated_state 夹具：把 state.STATE_DIR（SQLite 库目录）改到 tmp_path，测试结束关闭连接并自动还原。
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


def isolated_state(tmp_path, monkeypatch):
    """把 state.STATE_DIR（SQLite 库所在目录）改到 tmp_path/state，每个测试一份独立的库；
    结束时 storage.close_all() 释放连接。产出新的 STATE_DIR。"""
    from app import state, storage
    sd = tmp_path / 'state'
    sd.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(state, 'STATE_DIR', sd)
    monkeypatch.setattr(state, 'LOCK_FILE', tmp_path / state.LOCK_FILE.name)
    try:
        yield sd
    finally:
        storage.close_all()


if pytest is not None:
    isolated_state = pytest.fixture(isolated_state)
