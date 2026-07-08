"""扫描路径过滤工具。

提供 diff 快照、结果导出等文件系统扫描场景共用的默认排除规则。
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

DEFAULT_SCAN_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".claude",
        ".tg-uploads",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".cache",
        ".parcel-cache",
        ".turbo",
        ".next",
        ".nuxt",
        ".svelte-kit",
        "build",
        "dist",
        "coverage",
        "target",
        "out",
    }
)


def should_skip_default_scan_dir(path: Path, workdir_path: Path) -> bool:
    """判断目录是否命中默认扫描排除规则。"""
    try:
        rel_path = path.relative_to(workdir_path)
    except ValueError:
        return False
    return any(part in DEFAULT_SCAN_EXCLUDED_DIRS for part in rel_path.parts)


def should_skip_default_scan_file(path: Path, workdir_path: Path) -> bool:
    """判断文件是否位于默认排除目录内。"""
    try:
        rel_path = path.relative_to(workdir_path)
    except ValueError:
        return False
    return any(part in DEFAULT_SCAN_EXCLUDED_DIRS for part in rel_path.parts[:-1])


def matches_gitignore(path: Path, workdir_path: Path, patterns: list[str]) -> bool:
    """判断路径是否命中已解析的 gitignore 模式。"""
    try:
        rel_path = path.relative_to(workdir_path)
    except ValueError:
        return False

    rel_str = str(rel_path)
    name = path.name
    parts = rel_path.parts

    for pattern in patterns:
        clean_pattern = pattern.rstrip("/")
        if fnmatch.fnmatch(rel_str, pattern):
            return True
        if fnmatch.fnmatch(rel_str, clean_pattern):
            return True
        if fnmatch.fnmatch(name, clean_pattern):
            return True
        if pattern.endswith("/") and fnmatch.fnmatch(rel_str, clean_pattern):
            return True
        if pattern.startswith("/") and fnmatch.fnmatch(rel_str, pattern.lstrip("/")):
            return True
        if any(fnmatch.fnmatch(part, clean_pattern) for part in parts):
            return True
    return False


def should_skip_scan_dir(path: Path, workdir_path: Path, gitignore_patterns: list[str]) -> bool:
    """判断扫描时是否应跳过该目录。"""
    return should_skip_default_scan_dir(path, workdir_path) or matches_gitignore(path, workdir_path, gitignore_patterns)


def should_skip_scan_file(path: Path, workdir_path: Path, gitignore_patterns: list[str]) -> bool:
    """判断扫描时是否应跳过该文件。"""
    return should_skip_default_scan_file(path, workdir_path) or matches_gitignore(path, workdir_path, gitignore_patterns)
