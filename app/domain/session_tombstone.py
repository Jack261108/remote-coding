"""会话墓碑存储。

用于防止已结束或不可用的会话被重新发现或重复处理。
支持 TTL 自动过期机制，过期记录会在查询时自动清理。

该模块维护两类墓碑状态：
- **已结束（ended）**：会话正常退出或被用户主动关闭。
- **不可用（unavailable）**：会话因异常或超时而不可用。

墓碑记录会在以下场景中使用：
- 外部会话发现时跳过已墓碑化的会话。
- 自动审批服务跳过已墓碑化的会话。
- 会话绑定清理时判断会话状态。

使用方式::

    from app.domain.session_tombstone import SessionTombstoneStore

    store = SessionTombstoneStore(ttl_seconds=3600)
    store.mark_ended("session-123")
    if store.is_ended("session-123"):
        print("Session has ended")
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)


class SessionTombstoneStore:
    """会话墓碑存储。

    统一管理会话的结束/不可用状态，支持 TTL 自动过期。

    Parameters
    ----------
    ttl_seconds:
        墓碑记录的存活时间（秒），过期后自动清理。
    """

    def __init__(self, ttl_seconds: int = 3600) -> None:
        """初始化会话墓碑存储。

        Parameters
        ----------
        ttl_seconds:
            墓碑记录的存活时间（秒），过期后自动清理。默认 3600 秒（1 小时）。
        """
        self._ended: dict[str, datetime] = {}
        self._unavailable: dict[str, datetime] = {}
        self._ttl = timedelta(seconds=ttl_seconds)

    def mark_ended(self, session_id: str) -> None:
        """标记会话已结束。

        如果该会话之前被标记为不可用，会自动清除不可用标记。

        Parameters
        ----------
        session_id:
            会话标识符。
        """
        self._ended[session_id] = datetime.now(UTC)
        self._unavailable.pop(session_id, None)
        logger.debug("Marked session %s as ended", session_id)

    def mark_unavailable(self, session_id: str) -> None:
        """标记会话不可用。

        Parameters
        ----------
        session_id:
            会话标识符。
        """
        self._unavailable[session_id] = datetime.now(UTC)
        logger.debug("Marked session %s as unavailable", session_id)

    def is_ended(self, session_id: str) -> bool:
        """检查会话是否已结束。

        查询时自动检查 TTL，过期记录会被删除并返回 ``False``。

        Parameters
        ----------
        session_id:
            会话标识符。

        Returns
        -------
        bool
            会话是否在有效期内被标记为已结束。
        """
        if session_id not in self._ended:
            return False
        # 检查是否过期
        if datetime.now(UTC) - self._ended[session_id] > self._ttl:
            del self._ended[session_id]
            return False
        return True

    def is_unavailable(self, session_id: str) -> bool:
        """检查会话是否不可用。

        查询时自动检查 TTL，过期记录会被删除并返回 ``False``。

        Parameters
        ----------
        session_id:
            会话标识符。

        Returns
        -------
        bool
            会话是否在有效期内被标记为不可用。
        """
        if session_id not in self._unavailable:
            return False
        # 检查是否过期
        if datetime.now(UTC) - self._unavailable[session_id] > self._ttl:
            del self._unavailable[session_id]
            return False
        return True

    def clear(self, session_id: str) -> None:
        """清除会话的所有墓碑记录。

        同时清除已结束和不可用两种状态。

        Parameters
        ----------
        session_id:
            会话标识符。
        """
        self._ended.pop(session_id, None)
        self._unavailable.pop(session_id, None)

    def ended_ids(self) -> set[str]:
        """获取所有已结束的会话 ID。

        自动清理过期记录后再返回结果。

        Returns
        -------
        set[str]
            当前有效的已结束会话 ID 集合。
        """
        self._cleanup_expired_ended()
        return set(self._ended.keys())

    def unavailable_ids(self) -> set[str]:
        """获取所有不可用的会话 ID。

        自动清理过期记录后再返回结果。

        Returns
        -------
        set[str]
            当前有效的不可用会话 ID 集合。
        """
        self._cleanup_expired_unavailable()
        return set(self._unavailable.keys())

    def cleanup_expired(self) -> None:
        """清理所有过期的墓碑记录。

        同时清理已结束和不可用两类过期记录。
        该方法由周期性清理任务调用，也可手动调用。
        """
        self._cleanup_expired_ended()
        self._cleanup_expired_unavailable()

    def _cleanup_expired_ended(self) -> None:
        now = datetime.now(UTC)
        expired = [k for k, v in self._ended.items() if now - v > self._ttl]
        for k in expired:
            del self._ended[k]
            logger.debug("Cleaned up expired ended tombstone: %s", k)

    def _cleanup_expired_unavailable(self) -> None:
        now = datetime.now(UTC)
        expired = [k for k, v in self._unavailable.items() if now - v > self._ttl]
        for k in expired:
            del self._unavailable[k]
            logger.debug("Cleaned up expired unavailable tombstone: %s", k)
