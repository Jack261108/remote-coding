"""文件修改时间追踪工具。

提供文件修改时间（mtime）的缓存与变更检测功能，
用于高效判断文件是否被修改（例如触发会话同步或清理过期上传）。

核心功能：
- ``refresh_seen_mtimes()``：批量检查文件 mtime，返回变更的文件集合。
- ``clear_seen_mtimes_for_session()``：按会话 ID 清理 mtime 缓存。

使用方式::

    from app.infra.file_mtime_utils import refresh_seen_mtimes

    seen: dict[str, float] = {}
    changed = refresh_seen_mtimes(file_paths, seen)
    for path, mtime in changed.items():
        print(f"File changed: {path}")
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def refresh_seen_mtimes(
    paths: set[str],
    seen_mtimes: dict[str, float],
) -> dict[str, float]:
    """刷新文件修改时间缓存，返回变更的文件集合。

    遍历 ``paths`` 中的每个文件，获取其当前 mtime 与缓存比较。
    新文件或 mtime 发生变化的文件会被记录到返回值中，并更新缓存。
    不存在或无法访问的文件会被静默跳过。

    Parameters
    ----------
    paths:
        需要检查的文件路径集合。
    seen_mtimes:
        当前 mtime 缓存字典（会被就地修改）。

    Returns
    -------
    dict[str, float]
        mtime 发生变更的文件路径到新 mtime 的映射。
    """
    updated: dict[str, float] = {}
    for path in paths:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if path not in seen_mtimes or seen_mtimes[path] != mtime:
            updated[path] = mtime
    seen_mtimes.update(updated)
    return updated


def clear_seen_mtimes_for_session(
    session_id: str,
    seen_mtimes: dict[str, float],
) -> None:
    """清除指定会话的所有 mtime 缓存条目。

    遍历缓存字典，删除所有键中包含 ``session_id`` 的条目。
    用于会话结束时清理相关的文件监控缓存。

    Parameters
    ----------
    session_id:
        会话标识符，用作子串匹配过滤条件。
    seen_mtimes:
        需要清理的 mtime 缓存字典（会被就地修改）。
    """
    keys_to_remove = [k for k in seen_mtimes if session_id in k]
    for key in keys_to_remove:
        del seen_mtimes[key]
