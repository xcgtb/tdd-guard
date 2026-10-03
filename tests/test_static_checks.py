# -*- coding: utf-8 -*-
"""静态自检：防止 engine 拆分后「跨模块裸引用 / _eng 间接引用 / 双导入 / 属性不存在」再次混入。"""
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import static_check  # noqa: E402

# 迷你 app 包：state 有 STATE_DIR，core 有 esc，routers/deps 有 auth
_BASE = {
    'app/__init__.py': '',
    'app/core.py': 'def esc(x):\n    return x\n',
    'app/state.py': 'STATE_DIR = 1\n',
    'app/engine.py': 'X = 1\n',
    'app/main.py': 'from app import engine\n',
    'app/routers/__init__.py': '',
    'app/routers/deps.py': 'auth = 1\n',
}


def _run(files):
    d = Path(tempfile.mkdtemp())
    try:
        for rel, code in dict(_BASE, **files).items():
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(code, encoding='utf-8')
        return static_check.run(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _m(code):
    return _run({'app/m.py': code})


class TestStaticCheck:
    def test_repo_is_clean(self):
        assert static_check.run(ROOT) == []

    def test_base_fixture_is_clean(self):
        assert _run({}) == []

    # ── 未定义全局名 ──
    def test_detects_bare_cross_module_reference(self):
        issues = _m("def f():\n    return notify_telegram('x')\n")
        assert any('notify_telegram' in i for i in issues), issues

    def test_allows_imports_globals_and_docstrings(self):
        code = ('"""说明里提到 _eng() 没问题"""\nimport os\nX = 1\n'
                'def f():\n    global Y\n    Y = os.sep\n    return X, Y, len([])\n')
        assert _m(code) == []

    # ── 规则 a：_eng / _engine_ns 标识符 ──
    def test_detects_eng_identifier(self):
        issues = _m("def _eng():\n    return None\ndef f():\n    return _eng().X\n")
        assert any('_eng' in i for i in issues), issues
        issues = _m("def g(m):\n    return m._engine_ns\n")
        assert any('_engine_ns' in i for i in issues), issues

    def test_eng_inside_plain_string_is_not_identifier(self):
        assert _m("def f(x):\n    return x.get('engine_name')\n") == []

    # ── 规则 b：try/except ImportError 双导入 ──
    def test_detects_dual_import(self):
        issues = _m("try:\n    from . import state\nexcept ImportError:\n    import state\n")
        assert any('双导入' in i for i in issues), issues

    def test_allows_optional_third_party_import(self):
        assert _m("try:\n    import yaml\nexcept ImportError:\n    yaml = None\n") == []

    # ── 规则 c：import engine ──
    def test_detects_engine_import_in_business_module(self):
        for code in ("from . import engine\n", "import app.engine\n", "from app import engine\n"):
            issues = _m(code)
            assert any('engine 兼容壳' in i for i in issues), (code, issues)

    def test_allows_engine_import_in_main(self):
        assert _run({'app/main.py': 'from app import engine\nfrom app import state\n'}) == []

    # ── 规则 d：属性存在性 / 局部遮蔽 ──
    def test_detects_missing_module_attribute(self):
        issues = _m("from . import state\ndef f():\n    return state.NOPE\n")
        assert any('state.NOPE' in i for i in issues), issues
        issues = _run({'app/routers/r.py': "from app import state as st\ndef f():\n    return st.NOPE\n"})
        assert any('st.NOPE' in i for i in issues), issues

    def test_allows_existing_module_attribute(self):
        assert _m("from . import state\ndef f():\n    return state.STATE_DIR\n") == []

    def test_detects_local_shadowing_module_alias(self):
        issues = _m("from . import state\ndef f():\n    x = state.STATE_DIR\n    state = 2\n    return x, state\n")
        assert any('遮蔽' in i for i in issues), issues

    # ── 规则 e：from .mod import name 只允许 core / deps ──
    def test_detects_name_import_from_non_core(self):
        for code in ("from .state import STATE_DIR\n", "from app.state import STATE_DIR\n"):
            issues = _m(code)
            assert any('STATE_DIR' in i and '只允许' in i for i in issues), (code, issues)

    def test_allows_core_and_deps_name_imports(self):
        assert _m("from .core import esc\n") == []
        assert _run({'app/routers/r.py': "from app.routers.deps import auth\nfrom app import state\n"}) == []
