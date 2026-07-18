#!/usr/bin/env python3
"""Reject collection-time process, environment, and filesystem side effects."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = REPO_ROOT / "tests"

MUTATING_CALLS = {
    "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell",
    "builtins.exec",
    "importlib.reload",
    "os.chdir",
    "os.chmod",
    "os.chown",
    "os.fchmod",
    "os.fchown",
    "os.ftruncate",
    "os.link",
    "os.lchmod",
    "os.lchown",
    "os.mkdir",
    "os.makedirs",
    "os.open",
    "os.popen",
    "os.putenv",
    "os.remove",
    "os.removedirs",
    "os.rename",
    "os.renames",
    "os.replace",
    "os.rmdir",
    "os.symlink",
    "os.system",
    "os.truncate",
    "os.unlink",
    "os.unsetenv",
    "os.write",
    "os.environ.__delitem__",
    "os.environ.__setitem__",
    "os.environ.clear",
    "os.environ.pop",
    "os.environ.popitem",
    "os.environ.setdefault",
    "os.environ.update",
    "pty.spawn",
    "shutil.chown",
    "shutil.copy",
    "shutil.copy2",
    "shutil.copyfile",
    "shutil.copyfileobj",
    "shutil.copymode",
    "shutil.copystat",
    "shutil.copytree",
    "shutil.make_archive",
    "shutil.move",
    "shutil.rmtree",
    "shutil.unpack_archive",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "subprocess.getoutput",
    "subprocess.getstatusoutput",
    "subprocess.Popen",
    "subprocess.run",
    "sys.modules.__delitem__",
    "sys.modules.__setitem__",
    "sys.modules.clear",
    "sys.modules.pop",
    "sys.modules.popitem",
    "sys.modules.setdefault",
    "sys.modules.update",
    "tempfile.mkstemp",
    "tempfile.mkdtemp",
    "tempfile.NamedTemporaryFile",
    "tempfile.SpooledTemporaryFile",
    "tempfile.TemporaryDirectory",
    "tempfile.TemporaryFile",
}

PATH_IO_METHODS = {
    "chmod",
    "hardlink_to",
    "mkdir",
    "rename",
    "replace",
    "rmdir",
    "symlink_to",
    "touch",
    "unlink",
    "write_bytes",
    "write_text",
}

MUTABLE_MAPPINGS = {"os.environ", "sys.modules"}


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    operation: str


@dataclass(frozen=True)
class _Definition:
    path: Path
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    symbols: "_SymbolCollector"
    qualified: str
    parent: str | None
    owner_class: str | None


@dataclass(frozen=True)
class _DispatchTarget:
    qualified: str
    receivers: frozenset[str] | None = None


_AliasEnvironment = dict[str, tuple[_DispatchTarget, ...]]


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _module_name(path: Path) -> str:
    try:
        relative = path.resolve().relative_to(REPO_ROOT.resolve()).with_suffix("")
    except ValueError:
        return path.stem
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


class _SymbolCollector(ast.NodeVisitor):
    """Collect module imports, aliases, functions, and classes."""

    def __init__(self, module_name: str) -> None:
        self.module_name = module_name
        self.aliases = {
            "exec": "builtins.exec",
            "open": "builtins.open",
        }
        self.functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        self.classes: dict[str, ast.ClassDef] = {}

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local = alias.asname or alias.name.split(".", 1)[0]
            self.aliases[local] = alias.name if alias.asname else local

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = self._absolute_import(node)
        if not module:
            return
        for alias in node.names:
            if alias.name == "*":
                continue
            self.aliases[alias.asname or alias.name] = f"{module}.{alias.name}"

    def _absolute_import(self, node: ast.ImportFrom) -> str:
        if node.level == 0:
            return node.module or ""
        package = self.module_name.split(".")[:-1]
        remove = node.level - 1
        if remove > len(package):
            return ""
        prefix = package[: len(package) - remove]
        if node.module:
            prefix.extend(node.module.split("."))
        return ".".join(prefix)

    def visit_Assign(self, node: ast.Assign) -> None:
        value = self._assigned_value(node.value)
        if value:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.aliases[target.id] = value
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and node.value is not None:
            if value := self._assigned_value(node.value):
                self.aliases[node.target.id] = value
        if node.value is not None:
            self.visit(node.value)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        qualified = f"{self.module_name}.{node.name}"
        self.aliases[node.name] = qualified
        self.functions[qualified] = node
        self._visit_definition_expressions(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        qualified = f"{self.module_name}.{node.name}"
        self.aliases[node.name] = qualified
        self.functions[qualified] = node
        self._visit_definition_expressions(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualified = f"{self.module_name}.{node.name}"
        self.aliases[node.name] = qualified
        self.classes[qualified] = node
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def _visit_definition_expressions(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)

    def resolve(self, node: ast.AST) -> str:
        name = _dotted_name(node)
        if not name:
            return ""
        root, separator, remainder = name.partition(".")
        resolved = self.aliases.get(root, root)
        return f"{resolved}.{remainder}" if separator else resolved

    def _assigned_value(self, node: ast.AST) -> str:
        if isinstance(node, ast.Call):
            return self.resolve(node.func)
        return self.resolve(node)


class _DefinitionIndex:
    """Index functions, nested helpers, classes, methods, and inheritance."""

    def __init__(self) -> None:
        self.definitions: dict[str, _Definition] = {}
        self.classes: set[str] = set()
        self.class_bases: dict[str, tuple[str, ...]] = {}
        self.nested: dict[tuple[str, str], str] = {}

    def add_module(
        self,
        path: Path,
        tree: ast.Module,
        symbols: _SymbolCollector,
    ) -> None:
        self._walk(
            tree,
            path=path,
            symbols=symbols,
            parent=None,
            owner_class=None,
        )

    def _walk(
        self,
        node: ast.AST,
        *,
        path: Path,
        symbols: _SymbolCollector,
        parent: str | None,
        owner_class: str | None,
    ) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if parent is None:
                qualified = f"{symbols.module_name}.{node.name}"
            elif parent == owner_class:
                qualified = f"{parent}.{node.name}"
            else:
                qualified = f"{parent}.<locals>.{node.name}"
            definition = _Definition(
                path,
                node,
                symbols,
                qualified,
                parent,
                owner_class,
            )
            self.definitions[qualified] = definition
            if parent is not None:
                self.nested[(parent, node.name)] = qualified
            for child in node.body:
                self._walk(
                    child,
                    path=path,
                    symbols=symbols,
                    parent=qualified,
                    owner_class=owner_class,
                )
            return
        if isinstance(node, ast.ClassDef):
            if parent is None:
                qualified = f"{symbols.module_name}.{node.name}"
            elif parent == owner_class:
                qualified = f"{parent}.{node.name}"
            else:
                qualified = f"{parent}.<locals>.{node.name}"
            definition = _Definition(
                path,
                node,
                symbols,
                qualified,
                parent,
                qualified,
            )
            self.definitions[qualified] = definition
            self.classes.add(qualified)
            self.class_bases[qualified] = tuple(
                resolved
                for base in node.bases
                if (resolved := self._resolve_base(base, parent, symbols))
            )
            if parent is not None:
                self.nested[(parent, node.name)] = qualified
            for child in node.body:
                self._walk(
                    child,
                    path=path,
                    symbols=symbols,
                    parent=qualified,
                    owner_class=qualified,
                )
            return
        for child in ast.iter_child_nodes(node):
            self._walk(
                child,
                path=path,
                symbols=symbols,
                parent=parent,
                owner_class=owner_class,
            )

    def nested_helper(self, definition: _Definition, name: str) -> str | None:
        scope: _Definition | None = definition
        while scope is not None:
            if qualified := self.nested.get((scope.qualified, name)):
                return qualified
            scope = self.definitions.get(scope.parent or "")
        return None

    def _resolve_base(
        self,
        base: ast.AST,
        parent: str | None,
        symbols: _SymbolCollector,
    ) -> str:
        if isinstance(base, ast.Name):
            scope = parent
            while scope is not None:
                if qualified := self.nested.get((scope, base.id)):
                    return qualified
                definition = self.definitions.get(scope)
                scope = definition.parent if definition is not None else None
        return symbols.resolve(base)

    def mro(self, class_name: str) -> tuple[str, ...]:
        return self._mro(class_name, set())

    def _mro(self, class_name: str, active: set[str]) -> tuple[str, ...]:
        if class_name in active or class_name not in self.classes:
            return (class_name,)
        active.add(class_name)
        try:
            bases = self.class_bases.get(class_name, ())
            sequences = [list(self._mro(base, active)) for base in bases]
            sequences.append(list(bases))
            result = [class_name]
            while any(sequences):
                sequences = [sequence for sequence in sequences if sequence]
                candidate = next(
                    (
                        sequence[0]
                        for sequence in sequences
                        if all(sequence[0] not in other[1:] for other in sequences)
                    ),
                    None,
                )
                if candidate is None:
                    for sequence in sequences:
                        for fallback in sequence:
                            if fallback not in result:
                                result.append(fallback)
                    break
                if candidate not in result:
                    result.append(candidate)
                for sequence in sequences:
                    if sequence and sequence[0] == candidate:
                        sequence.pop(0)
            return tuple(result)
        finally:
            active.remove(class_name)

    def receiver_classes(self, owner_class: str) -> frozenset[str]:
        return frozenset(
            candidate
            for candidate in self.classes
            if owner_class in self.mro(candidate)
        ) or frozenset({owner_class})

    def method(
        self,
        class_name: str,
        method_name: str,
    ) -> str | None:
        for owner in self.mro(class_name):
            qualified = f"{owner}.{method_name}"
            if qualified in self.definitions:
                return qualified
        return None

    def super_method(
        self,
        receiver_class: str,
        defining_class: str,
        method_name: str,
    ) -> str | None:
        mro = self.mro(receiver_class)
        try:
            start = mro.index(defining_class) + 1
        except ValueError:
            start = 0
        for owner in mro[start:]:
            qualified = f"{owner}.{method_name}"
            if qualified in self.definitions:
                return qualified
        return None

    def method_binding(self, qualified: str) -> str:
        definition = self.definitions.get(qualified)
        if definition is None or not isinstance(
            definition.node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            return "instance"
        decorators = {
            definition.symbols.resolve(decorator).rsplit(".", 1)[-1]
            for decorator in definition.node.decorator_list
        }
        if "classmethod" in decorators:
            return "class"
        if "staticmethod" in decorators:
            return "static"
        return "instance"


class _EagerMutationVisitor(ast.NodeVisitor):
    """Visit expressions executed at import time and eagerly called helpers."""

    def __init__(
        self,
        path: Path,
        symbols: _SymbolCollector,
        index: _DefinitionIndex,
        active_helpers: set[tuple[str, tuple[str, ...]]] | None = None,
        current_definition: _Definition | None = None,
        effective_receivers: frozenset[str] | None = None,
        inherited_aliases: _AliasEnvironment | None = None,
    ) -> None:
        self.path = path
        self.symbols = symbols
        self.index = index
        self.findings: list[Finding] = []
        self._active_helpers = active_helpers if active_helpers is not None else set()
        self._current_definition = current_definition
        self._effective_receivers = effective_receivers
        self._local_aliases: _AliasEnvironment = dict(inherited_aliases or {})

    @staticmethod
    def _target_names(target: ast.AST) -> set[str]:
        if isinstance(target, ast.Name):
            return {target.id}
        if isinstance(target, (ast.List, ast.Tuple)):
            return {
                name
                for element in target.elts
                for name in _EagerMutationVisitor._target_names(element)
            }
        if isinstance(target, ast.Starred):
            return _EagerMutationVisitor._target_names(target.value)
        return set()

    def _clear_alias_targets(self, targets: list[ast.AST]) -> None:
        for target in targets:
            for name in self._target_names(target):
                self._local_aliases.pop(name, None)

    @staticmethod
    def _merge_aliases(*states: _AliasEnvironment) -> _AliasEnvironment:
        merged_targets: dict[str, dict[str, set[str] | None]] = {}
        for state in states:
            for name, dispatch_targets in state.items():
                by_qualified = merged_targets.setdefault(name, {})
                for target in dispatch_targets:
                    current = by_qualified.get(target.qualified)
                    if target.qualified not in by_qualified:
                        by_qualified[target.qualified] = (
                            None if target.receivers is None else set(target.receivers)
                        )
                    elif current is not None:
                        if target.receivers is None:
                            by_qualified[target.qualified] = None
                        else:
                            current.update(target.receivers)
        return {
            name: tuple(
                _DispatchTarget(
                    qualified,
                    None if receivers is None else frozenset(receivers),
                )
                for qualified, receivers in by_qualified.items()
            )
            for name, by_qualified in merged_targets.items()
        }

    def _visit_block(
        self,
        statements: list[ast.stmt],
        initial: _AliasEnvironment,
        *,
        collect_prefixes: bool = False,
    ) -> tuple[_AliasEnvironment, list[_AliasEnvironment]]:
        saved = self._local_aliases
        self._local_aliases = dict(initial)
        prefixes = [dict(self._local_aliases)] if collect_prefixes else []
        try:
            for statement in statements:
                self.visit(statement)
                if collect_prefixes:
                    prefixes.append(dict(self._local_aliases))
            return dict(self._local_aliases), prefixes
        finally:
            self._local_aliases = saved

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        initial = dict(self._local_aliases)
        body, _ = self._visit_block(node.body, initial)
        if node.orelse:
            alternate, _ = self._visit_block(node.orelse, initial)
        else:
            alternate = initial
        self._local_aliases = self._merge_aliases(body, alternate)

    def _visit_try(self, node: ast.Try | ast.TryStar) -> None:
        initial = dict(self._local_aliases)
        tried, prefixes = self._visit_block(
            node.body,
            initial,
            collect_prefixes=True,
        )
        normal, _ = self._visit_block(node.orelse, tried)
        outcomes = [normal]
        handler_initial = self._merge_aliases(*prefixes)
        for handler in node.handlers:
            if handler.type is not None:
                saved = self._local_aliases
                self._local_aliases = dict(handler_initial)
                try:
                    self.visit(handler.type)
                    handler_start = dict(self._local_aliases)
                finally:
                    self._local_aliases = saved
            else:
                handler_start = dict(handler_initial)
            if handler.name:
                handler_start.pop(handler.name, None)
            handled, _ = self._visit_block(handler.body, handler_start)
            outcomes.append(handled)
        merged = self._merge_aliases(*outcomes)
        if node.finalbody:
            merged, _ = self._visit_block(node.finalbody, merged)
        self._local_aliases = merged

    def visit_Try(self, node: ast.Try) -> None:
        self._visit_try(node)

    def visit_TryStar(self, node: ast.TryStar) -> None:
        self._visit_try(node)

    @staticmethod
    def _contains_loop_break(statements: list[ast.stmt]) -> bool:
        class _BreakVisitor(ast.NodeVisitor):
            found = False

            def visit_Break(self, node: ast.Break) -> None:
                self.found = True

            def visit_For(self, node: ast.For) -> None:
                return

            def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
                return

            def visit_While(self, node: ast.While) -> None:
                return

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                return

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                return

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                return

        visitor = _BreakVisitor()
        for statement in statements:
            visitor.visit(statement)
        return visitor.found

    def _visit_loop(
        self,
        body: list[ast.stmt],
        orelse: list[ast.stmt],
        *,
        target: ast.AST | None = None,
    ) -> None:
        initial = dict(self._local_aliases)
        loop_state = initial
        for _ in range(32):
            body_start = dict(loop_state)
            if target is not None:
                for name in self._target_names(target):
                    body_start.pop(name, None)
            body_result, _ = self._visit_block(body, body_start)
            updated = self._merge_aliases(initial, body_result)
            if updated == loop_state:
                break
            loop_state = updated
        if orelse:
            completed, _ = self._visit_block(orelse, loop_state)
            if self._contains_loop_break(body):
                loop_state = self._merge_aliases(loop_state, completed)
            else:
                loop_state = completed
        self._local_aliases = loop_state

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        self._visit_loop(node.body, node.orelse, target=node.target)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.visit(node.iter)
        self._visit_loop(node.body, node.orelse, target=node.target)

    def visit_While(self, node: ast.While) -> None:
        self.visit(node.test)
        self._visit_loop(node.body, node.orelse)

    @staticmethod
    def _match_names(pattern: ast.pattern) -> set[str]:
        names: set[str] = set()
        for child in ast.walk(pattern):
            if isinstance(child, ast.MatchAs) and child.name:
                names.add(child.name)
            elif isinstance(child, ast.MatchStar) and child.name:
                names.add(child.name)
            elif isinstance(child, ast.MatchMapping) and child.rest:
                names.add(child.rest)
        return names

    @staticmethod
    def _irrefutable_case(case: ast.match_case) -> bool:
        return (
            case.guard is None
            and isinstance(case.pattern, ast.MatchAs)
            and case.pattern.pattern is None
        )

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        initial = dict(self._local_aliases)
        outcomes: list[_AliasEnvironment] = []
        irrefutable = False
        for case in node.cases:
            case_start = dict(initial)
            for name in self._match_names(case.pattern):
                case_start.pop(name, None)
            saved = self._local_aliases
            self._local_aliases = case_start
            try:
                if case.guard is not None:
                    self.visit(case.guard)
                guarded = dict(self._local_aliases)
            finally:
                self._local_aliases = saved
            outcome, _ = self._visit_block(case.body, guarded)
            outcomes.append(outcome)
            irrefutable = irrefutable or self._irrefutable_case(case)
        if not irrefutable:
            outcomes.append(initial)
        self._local_aliases = self._merge_aliases(*outcomes)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_definition_expressions(node)
        self._bind_nested_definition(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_definition_expressions(node)
        self._bind_nested_definition(node.name)

    def _bind_nested_definition(self, name: str) -> None:
        if self._current_definition is None:
            return
        qualified = self.index.nested_helper(self._current_definition, name)
        if qualified is None:
            return
        self._local_aliases[name] = (_DispatchTarget(qualified),)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
            if not isinstance(decorator, ast.Call):
                self._visit_resolved_helper(self._resolve_callable(decorator))
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        for statement in node.body:
            self.visit(statement)
        self._bind_nested_definition(node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def _visit_definition_expressions(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
            if not isinstance(decorator, ast.Call):
                self._visit_resolved_helper(self._resolve_callable(decorator))
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)

    def visit_Assign(self, node: ast.Assign) -> None:
        self._check_targets(node.targets, node.lineno)
        self._clear_alias_targets(node.targets)
        self._record_local_aliases(node.targets, node.value)
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._check_targets([node.target], node.lineno)
        self._clear_alias_targets([node.target])
        if node.value is not None:
            self._record_local_aliases([node.target], node.value)
            self.visit(node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._check_targets([node.target], node.lineno)
        self._clear_alias_targets([node.target])
        self.visit(node.value)

    def visit_Delete(self, node: ast.Delete) -> None:
        self._check_targets(node.targets, node.lineno)
        self._clear_alias_targets(node.targets)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._clear_alias_targets([node.target])
        self._record_local_aliases([node.target], node.value)
        self.visit(node.value)

    def _check_targets(self, targets: list[ast.AST], line: int) -> None:
        for target in targets:
            candidate = target.value if isinstance(target, ast.Subscript) else target
            mapping = self.symbols.resolve(candidate)
            if mapping in MUTABLE_MAPPINGS:
                self.findings.append(Finding(self.path, line, f"mutation of {mapping}"))

    def _record_local_aliases(self, targets: list[ast.AST], value: ast.AST) -> None:
        if isinstance(value, ast.Call):
            qualified = self._resolve_callable(value.func)
            dispatch_targets = (_DispatchTarget(qualified),) if qualified else ()
        else:
            dispatch_targets = tuple(self._helper_targets(value))
            if isinstance(value, ast.Attribute):
                owner = self.symbols.resolve(value.value)
                if owner in self.index.classes:
                    dispatch_targets = tuple(
                        _DispatchTarget(target.qualified)
                        if self.index.method_binding(target.qualified) == "instance"
                        else target
                        for target in dispatch_targets
                    )
        if not dispatch_targets:
            return
        for target in targets:
            if isinstance(target, ast.Name):
                self._local_aliases[target.id] = dispatch_targets

    def visit_Call(self, node: ast.Call) -> None:
        name = self._callable_name(node.func)
        path_io = isinstance(node.func, ast.Attribute) and (
            node.func.attr in PATH_IO_METHODS
        )
        file_open = name in {"builtins.open", "io.open", "pathlib.Path.open"} or (
            isinstance(node.func, ast.Attribute) and node.func.attr == "open"
        )
        file_mutation = file_open and self._open_call_mutates(node, name)
        process_family = name.startswith(("os.exec", "os.spawn"))
        if name in MUTATING_CALLS or path_io or file_mutation or process_family:
            self.findings.append(Finding(self.path, node.lineno, f"call to {name}"))
        elif name in {"builtins.setattr", "setattr"}:
            self.findings.append(
                Finding(self.path, node.lineno, "module-scope setattr")
            )

        for target in self._helper_targets(node.func):
            self._visit_resolved_helper(target.qualified, target.receivers)
        self.generic_visit(node)

    @staticmethod
    def _open_call_mutates(node: ast.Call, name: str) -> bool:
        mode_node: ast.AST | None = None
        position = 0 if isinstance(node.func, ast.Attribute) else 1
        if len(node.args) > position:
            mode_node = node.args[position]
        for keyword in node.keywords:
            if keyword.arg == "mode":
                mode_node = keyword.value
        if mode_node is None:
            return False
        if not isinstance(mode_node, ast.Constant) or not isinstance(
            mode_node.value, str
        ):
            return True
        return any(flag in mode_node.value for flag in "wax+")

    def _visit_resolved_helper(
        self,
        qualified: str,
        receivers: frozenset[str] | None = None,
    ) -> None:
        if qualified in self.index.classes:
            receiver_classes = receivers or frozenset({qualified})
            constructors = {
                constructor
                for name in ("__new__", "__init__")
                if (constructor := self.index.method(qualified, name)) is not None
            }
            for constructor in sorted(constructors):
                self._visit_resolved_helper(constructor, receiver_classes)
            return
        definition = self.index.definitions.get(qualified)
        if definition is None:
            return
        lexical_closure = self._is_lexical_closure(definition)
        if (
            receivers is None
            and lexical_closure
            and self._effective_receivers is not None
        ):
            receivers = self._effective_receivers
        if receivers is None and definition.owner_class is not None:
            receivers = self.index.receiver_classes(definition.owner_class)
        active_key = (qualified, tuple(sorted(receivers or ())))
        if active_key in self._active_helpers:
            return
        self._active_helpers.add(active_key)
        try:
            nested = _EagerMutationVisitor(
                definition.path,
                definition.symbols,
                self.index,
                self._active_helpers,
                current_definition=definition,
                effective_receivers=receivers,
                inherited_aliases=(self._local_aliases if lexical_closure else None),
            )
            if isinstance(definition.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for argument in (
                    *definition.node.args.posonlyargs,
                    *definition.node.args.args,
                    *definition.node.args.kwonlyargs,
                ):
                    nested._local_aliases.pop(argument.arg, None)
                if definition.node.args.vararg is not None:
                    nested._local_aliases.pop(definition.node.args.vararg.arg, None)
                if definition.node.args.kwarg is not None:
                    nested._local_aliases.pop(definition.node.args.kwarg.arg, None)
            for statement in definition.node.body:
                nested.visit(statement)
            self.findings.extend(nested.findings)
        finally:
            self._active_helpers.remove(active_key)

    def _is_lexical_closure(self, definition: _Definition) -> bool:
        if self._current_definition is None:
            return False
        parent = definition.parent
        while parent is not None:
            if parent == self._current_definition.qualified:
                return True
            parent_definition = self.index.definitions.get(parent)
            parent = parent_definition.parent if parent_definition is not None else None
        return False

    def _helper_targets(
        self,
        node: ast.AST,
    ) -> list[_DispatchTarget]:
        targets: dict[str, set[str] | None] = {}

        def add(qualified: str, receivers: frozenset[str] | None = None) -> None:
            if not qualified:
                return
            if qualified not in targets:
                targets[qualified] = None if receivers is None else set(receivers)
            elif receivers is None:
                targets[qualified] = None
            elif targets[qualified] is not None:
                targets[qualified].update(receivers)

        if isinstance(node, ast.Name):
            if aliases := self._local_aliases.get(node.id):
                return list(aliases)

        if isinstance(node, ast.Attribute):
            owner_class = (
                self._current_definition.owner_class
                if self._current_definition is not None
                else None
            )
            if isinstance(node.value, ast.Name):
                owner_name = node.value.id
                if owner_name in {"self", "cls"} and owner_class is not None:
                    receivers = self._effective_receivers or (
                        self.index.receiver_classes(owner_class)
                    )
                    for receiver in receivers:
                        add(
                            self.index.method(receiver, node.attr) or "",
                            frozenset({receiver}),
                        )
                    return self._group_targets(targets)
                if local_targets := self._local_aliases.get(owner_name):
                    for local_target in local_targets:
                        local_owner = local_target.qualified
                        add(
                            self.index.method(local_owner, node.attr) or "",
                            frozenset({local_owner}),
                        )
                    if targets:
                        return self._group_targets(targets)
            if isinstance(node.value, ast.Call):
                called = self._resolve_callable(node.value.func)
                if called in {"super", "builtins.super"} and owner_class is not None:
                    receivers = self._effective_receivers or (
                        self.index.receiver_classes(owner_class)
                    )
                    for receiver in receivers:
                        add(
                            self.index.super_method(receiver, owner_class, node.attr)
                            or "",
                            frozenset({receiver}),
                        )
                    return self._group_targets(targets)
                if called in self.index.classes:
                    add(
                        self.index.method(called, node.attr) or "",
                        frozenset({called}),
                    )
                    return self._group_targets(targets)
            resolved = self.symbols.resolve(node)
            owner, separator, method_name = resolved.rpartition(".")
            if separator and owner in self.index.classes:
                add(
                    self.index.method(owner, method_name) or resolved,
                    frozenset({owner}),
                )
                return self._group_targets(targets)

        add(self._resolve_callable(node))
        return self._group_targets(targets)

    @staticmethod
    def _group_targets(
        targets: dict[str, set[str] | None],
    ) -> list[_DispatchTarget]:
        return [
            _DispatchTarget(
                qualified,
                None if receivers is None else frozenset(receivers),
            )
            for qualified, receivers in targets.items()
        ]

    def _resolve_callable(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            if local_targets := self._local_aliases.get(node.id):
                return local_targets[0].qualified
            if self._current_definition is not None:
                if nested := self.index.nested_helper(
                    self._current_definition, node.id
                ):
                    return nested
            return self.symbols.resolve(node)
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                owner_name = node.value.id
                owner_class = (
                    self._current_definition.owner_class
                    if self._current_definition is not None
                    else None
                )
                if owner_name in {"self", "cls"} and owner_class is not None:
                    return self.index.method(owner_class, node.attr) or (
                        f"{owner_class}.{node.attr}"
                    )
                if local_targets := self._local_aliases.get(owner_name):
                    local_owner = local_targets[0].qualified
                    return self.index.method(local_owner, node.attr) or (
                        f"{local_owner}.{node.attr}"
                    )
            if isinstance(node.value, ast.Call):
                called = self._resolve_callable(node.value.func)
                if called in {"super", "builtins.super"}:
                    owner_class = (
                        self._current_definition.owner_class
                        if self._current_definition is not None
                        else None
                    )
                    if owner_class is not None:
                        for base in self.index.class_bases.get(owner_class, ()):
                            if method := self.index.method(base, node.attr):
                                return method
                if called in self.index.classes:
                    return self.index.method(called, node.attr) or (
                        f"{called}.{node.attr}"
                    )
                return f"{called}.{node.attr}" if called else node.attr
            resolved = self.symbols.resolve(node)
            owner, separator, method_name = resolved.rpartition(".")
            if separator and owner in self.index.classes:
                return self.index.method(owner, method_name) or resolved
            return resolved
        return self.symbols.resolve(node)

    def _callable_name(self, node: ast.AST) -> str:
        return self._resolve_callable(node)


def inspect_files(paths: list[Path]) -> list[Finding]:
    modules: list[tuple[Path, ast.Module, _SymbolCollector]] = []
    index = _DefinitionIndex()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        symbols = _SymbolCollector(_module_name(path))
        symbols.visit(tree)
        modules.append((path, tree, symbols))
        index.add_module(path, tree, symbols)

    findings: list[Finding] = []
    for path, tree, symbols in modules:
        visitor = _EagerMutationVisitor(path, symbols, index)
        visitor.visit(tree)
        findings.extend(visitor.findings)
    return findings


def inspect_file(path: Path) -> list[Finding]:
    return inspect_files([path])


def test_python_files(test_root: Path = TEST_ROOT) -> list[Path]:
    return sorted(
        path for path in test_root.rglob("*.py") if "__pycache__" not in path.parts
    )


def main() -> int:
    findings = inspect_files(test_python_files())
    if findings:
        for finding in sorted(
            set(findings), key=lambda item: (str(item.path), item.line, item.operation)
        ):
            relative = finding.path.relative_to(REPO_ROOT)
            print(f"{relative}:{finding.line}: {finding.operation}")
        print("move collection-time side effects into a scoped pytest fixture")
        return 1
    print("test isolation static check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
