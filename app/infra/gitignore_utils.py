"""Gitignore 模式加载工具。

从工作目录的 ``.gitignore`` 文件中解析忽略模式，
用于文件过滤（如上传文件时排除被 gitignore 忽略的文件）。

核心功能：
- 逐行解析 ``.gitignore`` 文件，跳过空行和注释。
- 文件不存在或读取失败时返回空列表，不抛出异常。

使用方式::

    from app.infra.gitignore_utils import load_gitignore_patterns

    patterns = load_gitignore_patterns("/path/to/workdir")
    # patterns = ["*.pyc", "__pycache__/", ".env", ...]
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load_gitignore_patterns(workdir: str) -> list[str]:
    """从工作目录加载 gitignore 忽略模式。

    读取 ``workdir/.gitignore`` 文件，逐行解析忽略模式。
    跳过空行和以 ``#`` 开头的注释行。

    Parameters
    ----------
    workdir:
        工作目录路径。

    Returns
    -------
    list[str]
        忽略模式列表。文件不存在或读取失败时返回空列表。
    """
    gitignore_path = Path(workdir) / ".gitignore"
    if not gitignore_path.is_file():
        return []

    patterns: list[str] = []
    try:
        for line in gitignore_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            patterns.append(line)
    except OSError:
        logger.warning("Failed to read .gitignore at %s", gitignore_path)

    return patterns
