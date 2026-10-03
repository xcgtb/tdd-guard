#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静态自检（无第三方依赖），专门防 engine 拆分后「跨模块裸引用 / 间接引用回潮」这一类运行时才炸的错误。

检查项（扫描 app/ 下全部 .py，含子目录）：
  1. 未定义的全局名：函数里引用了既不是本模块定义/导入、也不是内置的名字
     （v1.7.5 拆分遗留的 emby_lib_of / Tmdb / notify_telegram 等 NameError 就是这一类，
      而且常被外层 `except` 吞掉，表现为数据静默归零）。
  2. 出现 `_eng` / `_engine_ns` 标识符：engine 已是兼容壳，跨模块一律 `模块.名字` 访问。
  3. `try: from . import x / except ImportError:` 双导入：app 内只允许包导入。
  4. `import engine`：业务模块不得依赖兼容壳，只有 app/engine.py 与 app/main.py 例外。
  5. 属性存在性：`from . import X` / `from app import X`（含 as 别名）绑定的模块，
     `X.attr` 必须是 app/X.py 顶层定义的名字（赋值/def/class/import/global 赋值）；
     同时检查函数里局部变量遮蔽了模块别名（`state = ...` 后再用 `state.STATE_DIR`）。
  6. `from .mod import name` 只允许 core（routers 另可 `from app.routers.deps import ...`），
     其余模块一律 `from . import mod` 后在调用时取 `mod.name`（monkeypatch / reload_config 才能穿透）。

用法：python3 scripts/static_check.py        （有问题退出码 1）
"""
import ast
import builtins
import symtable
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE_DUNDERS = {'__file__', '__name__', '__doc__', '__package__', '__spec__', '__path__'}
BANNED_IDENTS = {'_eng', '_engine_ns'}
ENGINE_ALLOWED = {'app/engine.py', 'app/main.py'}
NAME_IMPORT_ALLOWED = {'app.core', 'app.routers.deps'}


def _module_defined(top, tree):
    names = set(dir(builtins)) | MODULE_DUNDERS
    for sym in top.get_symbols():
        if sym.is_assigned() or sym.is_imported() or sym.is_namespace():
            names.add(sym.get_name())
    # 函数内 `global X` 后赋值，同样定义了模块级 X
    def walk(tab):
        for sym in tab.get_symbols():
            if sym.is_declared_global() and sym.is_assigned():
                names.add(sym.get_name())
        for ch in tab.get_children():
            walk(ch)
    walk(top)
    return names


def _undefined_globals(src, path):
    tree = ast.parse(src, str(path))
    top = symtable.symtable(src, str(path), 'exec')
    defined = _module_defined(top, tree)
    bad = []

    def walk(tab):
        if tab is not top:
            for sym in tab.get_symbols():
                if sym.is_referenced() and sym.is_global() and sym.get_name() not in defined:
                    bad.append((tab.get_name(), sym.get_name()))
        for ch in tab.get_children():
            walk(ch)
    walk(top)
    return bad


def _locate(tree, name):
    """尽力给出行号（第一处引用）。"""
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load):
            return n.lineno
    return 0


# ── 模块解析 ──

def _pkg_of(rel_parts):
    """app/routers/x.py -> ('app', 'routers')"""
    return tuple(rel_parts[:-1])


def _resolve(pkg, node):
    """ImportFrom 的目标包（点分元组）；不是 app 内的返回 None。"""
    if node.level:
        base = pkg[:len(pkg) - (node.level - 1)] if node.level > 1 else pkg
        return base + (tuple(node.module.split('.')) if node.module else ())
    mod = tuple((node.module or '').split('.'))
    return mod if mod and mod[0] == 'app' else None


def _is_module(root, dotted):
    p = Path(root).joinpath(*dotted)
    return p.with_suffix('.py').is_file() or (p / '__init__.py').is_file()


def _toplevel_names(path, cache):
    if path in cache:
        return cache[path]
    names = set(MODULE_DUNDERS)
    try:
        src = path.read_text(encoding='utf-8')
        tree = ast.parse(src)
        top = symtable.symtable(src, str(path), 'exec')
        names |= {s.get_name() for s in top.get_symbols() if s.is_assigned() or s.is_imported() or s.is_namespace()}
        names |= _module_defined(top, tree) - set(dir(builtins))
    except (OSError, SyntaxError):
        names = None
    cache[path] = names
    return names


def _is_app_import(node, pkg, root):
    if isinstance(node, ast.ImportFrom):
        return node.level > 0 or _resolve(pkg, node) is not None
    if isinstance(node, ast.Import):
        for a in node.names:
            top = a.name.split('.')[0]
            if top == 'app' or (Path(root) / 'app' / (top + '.py')).is_file():
                return True
    return False


def _check_file(path, root, cache):
    rel = path.relative_to(root)
    rel_s = rel.as_posix()
    pkg = _pkg_of(rel.parts)
    src = path.read_text(encoding='utf-8')
    tree = ast.parse(src, str(path))
    issues = []

    def add(ln, msg):
        issues.append('%s:%d %s' % (rel_s, ln, msg))

    # 1. 未定义全局名
    for scope, name in _undefined_globals(src, path):
        add(_locate(tree, name), '未定义的全局名 %r（位于 %s）' % (name, scope))

    aliases = {}  # 别名 -> 模块文件路径（engine 兼容壳不登记）
    for n in ast.walk(tree):
        # 2. _eng / _engine_ns 标识符
        ident = None
        if isinstance(n, ast.Name):
            ident = n.id
        elif isinstance(n, ast.Attribute):
            ident = n.attr
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            ident = n.name
        elif isinstance(n, ast.alias):
            ident = n.asname or n.name.split('.')[-1]
        if ident in BANNED_IDENTS:
            add(getattr(n, 'lineno', 0), '出现 %s（engine 已是兼容壳，请直接用 `模块.名字`）' % ident)

        # 3. try/except ImportError 双导入
        if isinstance(n, ast.Try):
            catches = any(
                h.type is not None and any(isinstance(x, ast.Name) and x.id in ('ImportError', 'ModuleNotFoundError')
                                           for x in ([h.type] + list(getattr(h.type, 'elts', []))))
                for h in n.handlers)
            if catches and any(_is_app_import(s, pkg, root) for s in n.body):
                add(n.lineno, 'try/except ImportError 双导入 app 模块（app 内只允许包导入）')

        # 4. import engine
        if isinstance(n, (ast.Import, ast.ImportFrom)) and rel_s not in ENGINE_ALLOWED:
            targets = []
            if isinstance(n, ast.Import):
                targets = [a.name for a in n.names]
            else:
                tgt = _resolve(pkg, n)
                if tgt is not None or n.level:
                    targets = ['.'.join((tgt or ()) + (a.name,)) for a in n.names] + ['.'.join(tgt or ())]
            if any(t in ('engine', 'app.engine') for t in targets):
                add(n.lineno, '导入了 engine 兼容壳（只有 app/engine.py、app/main.py 可以）')

        # 5/6. from X import name
        if isinstance(n, ast.ImportFrom):
            tgt = _resolve(pkg, n)
            if tgt is None:
                continue
            for a in n.names:
                if _is_module(root, tgt + (a.name,)):
                    if a.name != 'engine':
                        aliases[a.asname or a.name] = Path(root).joinpath(*tgt, a.name).with_suffix('.py')
                elif '.'.join(tgt) not in NAME_IMPORT_ALLOWED:
                    add(n.lineno, '`from %s import %s`：只允许从 core（routers 另可 deps）按名字导入，'
                        '其余请 `from . import 模块` 后用 `模块.%s`' % ('.'.join(tgt), a.name, a.name))
        elif isinstance(n, ast.Import):
            for a in n.names:
                parts = tuple(a.name.split('.'))
                if a.asname and parts[0] == 'app' and parts[-1] != 'engine' and _is_module(root, parts):
                    aliases[a.asname] = Path(root).joinpath(*parts).with_suffix('.py')

    # 5. 属性存在性 + 局部遮蔽
    if aliases:
        names_of = {al: _toplevel_names(p, cache) for al, p in aliases.items() if p.is_file()}
        top = symtable.symtable(src, str(path), 'exec')
        shadowed = {}  # 作用域起始行 -> 被局部化的别名集合

        def walk(tab):
            if tab.get_type() != 'module':
                loc = set()
                for al in names_of:
                    try:
                        s = tab.lookup(al)
                    except KeyError:
                        continue
                    if not s.is_global() and (s.is_local() or s.is_free() or s.is_parameter()):
                        loc.add(al)
                if loc:
                    shadowed.setdefault(tab.get_lineno(), set()).update(loc)
            for ch in tab.get_children():
                walk(ch)
        walk(top)

        def visit(node, local):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
                                 ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                local = local | shadowed.get(node.lineno, set())
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in names_of:
                al, attr = node.value.id, node.attr
                defined = names_of[al]
                if al in local:
                    if defined and attr in defined:
                        add(node.lineno, '%s.%s：模块别名 %s 在函数内被局部变量遮蔽' % (al, attr, al))
                elif defined is not None and attr not in defined:
                    add(node.lineno, '%s.%s 不存在（%s 顶层没有定义 %s）' % (al, attr, aliases[al].name, attr))
            for ch in ast.iter_child_nodes(node):
                visit(ch, local)
        visit(tree, frozenset())
    return issues


def run(root=ROOT):
    root = Path(root)
    issues = []
    cache = {}
    for path in sorted((root / 'app').rglob('*.py')):
        issues.extend(_check_file(path, root, cache))
    return issues


if __name__ == '__main__':
    problems = run()
    if problems:
        print('\n'.join(problems))
        sys.exit(1)
    print('static check: OK')
