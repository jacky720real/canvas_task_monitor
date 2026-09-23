"""验证业务层不依赖任何宿主 SDK。

规则：src/canvas_task_monitor/ 下所有 .py 文件的 import 语句
不得包含以下关键字：mcp / dsh / deepseek / click / argparse。
实现方式：用 ast 遍历每个文件，收集 Import / ImportFrom 节点。
违规时 pytest fail 并打印违规文件与行号。

这是"抗宿主破坏性更新"架构约束的执行器：只要它绿，宿主换版本就不会波及业务层。
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "canvas_task_monitor"
FORBIDDEN_PREFIXES = ("mcp", "dsh", "deepseek", "click", "argparse")


def _source_files() -> list[Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def _iter_imports(tree: ast.AST) -> Iterator[tuple[int, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            yield node.lineno, node.module or ""


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: str(p.name))
def test_no_host_sdk_import_in_package(path: Path) -> None:
    """业务层任何文件都不得 import 宿主 SDK。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    violations = [
        f"{path.relative_to(PACKAGE_ROOT)}:{lineno} → {module}"
        for lineno, module in _iter_imports(tree)
        if module.split(".")[0] in FORBIDDEN_PREFIXES
    ]

    assert not violations, "业务层出现宿主 SDK 依赖：\n" + "\n".join(violations)


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: str(p.name))
def test_package_uses_relative_imports(path: Path) -> None:
    """包内模块必须用相对导入（避免"同一模块被加载两次"）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    violations = [
        f"{path.relative_to(PACKAGE_ROOT)}:{lineno} → {module}"
        for lineno, module in _iter_imports(tree)
        if module.startswith("canvas_task_monitor")
    ]

    assert not violations, "包内出现绝对自引用，请改为相对导入：\n" + "\n".join(violations)


def test_package_has_no_src_prefix_imports() -> None:
    """禁止 `from src.canvas_task_monitor...` 反模式。"""
    offenders = [
        str(path.relative_to(PACKAGE_ROOT))
        for path in _source_files()
        if "src.canvas_task_monitor" in path.read_text(encoding="utf-8")
    ]

    assert not offenders, f"发现 src. 前缀反模式：{offenders}"


ENTRY_FILES = ("cli_main.py", "mcp_server.py", "dsh_plugin.py", "web_main.py")

# web_main.py 允许 import 的模块白名单（规格：零新依赖，只用标准库 + Container）
WEB_ENTRY_ALLOWED_MODULES = {
    "__future__",
    "argparse",
    "asyncio",
    "http.server",
    "json",
    "pathlib",
    "sys",
    "typing",
    "urllib.parse",
    "webbrowser",
    "canvas_task_monitor.services.bootstrap",
}

BUSINESS_SYMBOLS = {
    "TaskService",
    "TaskRepo",
    "TaskExtractor",
    "SnapshotRepo",
    "ChangeRepo",
    "Database",
    "TaskItem",
    "RawItem",
    "ChangeRecord",
    "CanvasConnector",
    "GraphMailConnector",
    "ImapMailConnector",
    "Poller",
    "detect_changes",
    "canonical_hash",
    "AppConfig",
    "PromptTemplate",
    "OpenAICompatClient",
    "task_row_to_dict",
}


def test_entry_files_exist_at_project_root() -> None:
    """四个入口文件都必须在项目根（适配层唯一允许感知宿主的地方）。"""
    root = PACKAGE_ROOT.parents[1]

    for name in ENTRY_FILES:
        assert (root / name).is_file(), f"缺少入口文件：{name}"


def test_web_entry_is_thin_adapter() -> None:
    """web_main.py 只准 import 白名单模块，且一律通过 facade.invoke 调业务能力。"""
    path = PACKAGE_ROOT.parents[1] / "web_main.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    modules = {module for _, module in _iter_imports(tree)}
    extra = sorted(modules - WEB_ENTRY_ALLOWED_MODULES)
    assert not extra, f"web_main.py 出现白名单外的 import：{extra}"

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported.add(alias.asname or alias.name)

    violations = sorted(imported & BUSINESS_SYMBOLS)
    assert not violations, f"web_main.py 依赖了业务/领域符号：{violations}"

    # 三个业务动作都必须过 facade（list_tasks / get_task / mark_task）
    assert source.count("facade.invoke(") >= 3
    # 刻意不暴露轮询端点（会烧 token）
    assert 'parsed.path == "/api/poll"' not in source
