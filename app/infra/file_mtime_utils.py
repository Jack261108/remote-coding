"""文件修改时间缓存工具。"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


async def refresh_seen_mtimes(
    paths: set[str],
    seen_mtimes: dict[str, float],
) -> dict[str, float]:
    """刷新文件修改时间缓存并返回发生变化的条目。

    遍历 ``paths`` 中的文件路径，读取当前文件修改时间（mtime），并与
    ``seen_mtimes`` 中已缓存的值比较。新文件或 mtime 发生变化的文件会被
    写入 ``seen_mtimes``，同时以 ``{path: mtime}`` 形式返回。无法访问或
    不存在的文件会被跳过。

    Args:
        paths: 需要检查的文件路径集合。
        seen_mtimes: 已缓存的文件修改时间字典，会被就地更新。

    Returns:
        mtime 发生变化的文件路径到新 mtime 的映射。
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


async def clear_seen_mtimes(
    paths: set[str],
    seen_mtimes: dict[str, float],
) -> dict[str, float]:
    """按文件路径集合清除修改时间缓存并返回被清除的条目。

    遍历 ``paths`` 中的文件路径，删除 ``seen_mtimes`` 中已存在的对应缓存。
    该函数只处理明确给出的路径，不会按前缀或子串匹配额外条目。

    Args:
        paths: 需要从缓存中清除的文件路径集合。
        seen_mtimes: 已缓存的文件修改时间字典，会被就地修改。

    Returns:
        被清除的文件路径到原 mtime 的映射。
    """
    removed: dict[str, float] = {path: seen_mtimes[path] for path in paths if path in seen_mtimes}
    for path in removed:
        del seen_mtimes[path]
    return removed


async def clear_seen_mtimes_for_session(
    session_id: str,
    seen_mtimes: dict[str, float],
) -> None:
    """清除指定会话关联的文件修改时间缓存。

    删除 ``seen_mtimes`` 中键名包含 ``session_id`` 的所有条目，用于会话结束
    或停止监控时释放该会话的 mtime 缓存。

    Args:
        session_id: 需要清理缓存的会话 ID。
        seen_mtimes: 已缓存的文件修改时间字典，会被就地修改。
    """
    keys_to_remove = {key for key in seen_mtimes if session_id in key}
    await clear_seen_mtimes(keys_to_remove, seen_mtimes)
