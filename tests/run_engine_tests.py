#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
极简测试 runner，不依赖 pytest。

用法:
    python3 tests/run_engine_tests.py

规则:
  - 收集 tests/ 下所有 test_*.py 模块
  - 遍历其中所有 `Test*` 类
  - 跑每个 `test_*` 方法
  - 打印 PASS / FAIL / ERROR
  - 退出码: 0=全通过, 1=有失败
"""
import importlib
import inspect
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))       # 让 `from app import ...` 能用
sys.path.insert(0, str(HERE))              # 让 test_*.py 能被 import
import conftest  # noqa: E402,F401  先于任何 app 模块设置测试环境变量（与 pytest 一致）


def _iter_test_modules():
    for p in sorted(HERE.glob('test_*.py')):
        yield p.stem


def _run_class(cls):
    inst = cls()
    passed = failed = errored = 0
    for name in sorted(dir(inst)):
        if not name.startswith('test_'):
            continue
        method = getattr(inst, name)
        if not callable(method):
            continue
        try:
            method()
            print('  PASS  %s.%s' % (cls.__name__, name))
            passed += 1
        except AssertionError as e:
            print('  FAIL  %s.%s' % (cls.__name__, name))
            tb = traceback.format_exc()
            for line in tb.rstrip().splitlines():
                print('        ' + line)
            failed += 1
        except Exception as e:
            print('  ERROR %s.%s: %s' % (cls.__name__, name, e))
            tb = traceback.format_exc()
            for line in tb.rstrip().splitlines():
                print('        ' + line)
            errored += 1
    return passed, failed, errored


def main():
    total_p = total_f = total_e = 0
    for modname in _iter_test_modules():
        print('')
        print('=== %s ===' % modname)
        try:
            mod = importlib.import_module(modname)
        except Exception as e:
            print('  ERROR 无法 import: %s' % e)
            traceback.print_exc()
            total_e += 1
            continue

        found = False
        for _, cls in inspect.getmembers(mod, inspect.isclass):
            if cls.__module__ != mod.__name__:
                continue
            if not cls.__name__.startswith('Test'):
                continue
            found = True
            p, f, e = _run_class(cls)
            total_p += p; total_f += f; total_e += e

        if not found:
            print('  (无测试类)')

    print('')
    print('=' * 50)
    print('总计: %d passed, %d failed, %d errored' % (total_p, total_f, total_e))
    print('=' * 50)
    sys.exit(0 if (total_f == 0 and total_e == 0) else 1)


if __name__ == '__main__':
    main()
