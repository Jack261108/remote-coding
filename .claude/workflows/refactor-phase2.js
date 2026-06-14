export const meta = {
  name: 'refactor-phase2',
  description: 'ultracode 重构第二阶段：中间件应用、后台任务基类、墓碑存储',
  phases: [
    { title: '应用 SessionGuardMiddleware' },
    { title: '实现 ErrorHandlingMiddleware' },
    { title: '实现 CallbackValidatorMiddleware' },
    { title: '提取 PeriodicBackgroundTask 基类' },
    { title: '提取 SessionTombstoneStore' },
    { title: '验证测试' },
  ],
}

// 阶段 1: 应用 SessionGuardMiddleware 到更多处理器
phase('应用 SessionGuardMiddleware')

await agent(
  `将 SessionGuardMiddleware 应用到以下处理器，替换重复的会话检查代码：

  目标处理器：
  1. app/bot/handlers/command_cmds.py - 两处会话检查（message handler 和 callback handler）
  2. app/bot/handlers/command_resume.py - 两处会话检查（message handler 和 callback handler）
  3. app/bot/handlers/file_upload.py - 两处会话检查
  4. app/bot/router.py - command_claude_chat_text 处理器

  步骤：
  1. 在 app/bot/router.py 中创建两个 SessionGuardMiddleware 实例：
     - guard_basic = SessionGuardMiddleware(session_service, require_active=False)
     - guard_active = SessionGuardMiddleware(session_service, require_active=True)
  2. 将 guard_active 应用到需要 claude_chat_active 的处理器：
     - command_cmds 的 message 和 callback handler
     - command_resume 的 message 和 callback handler
     - router.command_claude_chat_text
  3. 将 guard_basic 应用到只需要 session 存在的处理器：
     - file_upload 的 document 和 photo handler
  4. 删除处理器中重复的会话检查代码（session = await session_service.get(user_id); if session is None: ...）

  注意：
  - 保留 skip_commands 参数用于跳过特定命令
  - 确保 data['session'] 能正确传递到处理器
  - 处理器中需要从 data 获取 session 而不是重新查询

  执行修改操作。
  `,
  { label: 'apply-session-guard', phase: '应用 SessionGuardMiddleware' }
)

// 阶段 2: 实现 ErrorHandlingMiddleware
phase('实现 ErrorHandlingMiddleware')

await agent(
  `创建并应用 ErrorHandlingMiddleware 统一错误处理。

  步骤 1: 创建 app/bot/middleware/error_handling.py
  \`\`\`python
  """统一错误处理中间件。"""
  from __future__ import annotations

  import logging
  from collections.abc import Awaitable, Callable
  from typing import Any

  from aiogram import BaseMiddleware
  from aiogram.types import CallbackQuery, Message

  logger = logging.getLogger(__name__)


  class ErrorHandlingMiddleware(BaseMiddleware):
      """统一错误处理中间件。

      捕获 handler 中抛出的异常：
      - ValueError: 记录 warning，回复用户友好的错误消息
      - Exception: 记录 exception，回复通用错误消息
      """

      async def __call__(
          self,
          handler: Callable[[Any, dict], Awaitable],
          event: Any,
          data: dict,
      ) -> Any:
          try:
              return await handler(event, data)
          except ValueError as exc:
              logger.warning("Handler error: %s", exc)
              error_msg = f"操作失败: {exc}"
              if isinstance(event, Message):
                  await event.answer(error_msg)
              elif isinstance(event, CallbackQuery):
                  await event.answer(error_msg, show_alert=True)
              return None
          except Exception as exc:
              logger.exception("Handler exception")
              error_msg = "发生内部错误，请稍后重试"
              if isinstance(event, Message):
                  await event.answer(error_msg)
              elif isinstance(event, CallbackQuery):
                  await event.answer(error_msg, show_alert=True)
              return None
  \`\`\`

  步骤 2: 在 app/bot/router.py 中注册中间件
  - 在 router 创建时添加 ErrorHandlingMiddleware
  - 确保中间件顺序正确（error_handling 在最外层）

  步骤 3: 移除处理器中重复的 try-except 代码
  目标处理器：
  - app/bot/handlers/command_claude.py 中的 try-except ValueError + Exception
  - app/bot/handlers/command_run.py 中的 try-except ValueError + Exception
  - app/bot/handlers/command_resume.py 中的 try-except
  - app/bot/handlers/command_export.py 中的 try-except

  执行创建和修改操作。
  `,
  { label: 'create-error-middleware', phase: '实现 ErrorHandlingMiddleware' }
)

// 阶段 3: 实现 CallbackValidatorMiddleware
phase('实现 CallbackValidatorMiddleware')

await agent(
  `创建并应用 CallbackValidatorMiddleware 统一回调验证。

  步骤 1: 创建 app/bot/middleware/callback_validator.py
  \`\`\`python
  """回调数据验证中间件。"""
  from __future__ import annotations

  import logging
  from collections.abc import Awaitable, Callable
  from typing import Any, Optional

  from aiogram import BaseMiddleware
  from aiogram.types import CallbackQuery

  logger = logging.getLogger(__name__)


  class CallbackValidatorMiddleware(BaseMiddleware):
      """回调数据验证中间件。

      Parameters
      ----------
      expected_parts:
          callback data 按 ':' 拆分后期望的段数。
      prefix:
          可选，首段必须以此前缀开头。
      """

      def __init__(
          self,
          expected_parts: int,
          prefix: Optional[str] = None,
      ) -> None:
          super().__init__()
          self._expected_parts = expected_parts
          self._prefix = prefix

      async def __call__(
          self,
          handler: Callable[[CallbackQuery, dict], Awaitable],
          event: CallbackQuery,
          data: dict,
      ) -> Any:
          if not event.data:
              await event.answer("无效的回调数据", show_alert=True)
              return None

          parts = event.data.split(":")

          if len(parts) != self._expected_parts:
              await event.answer("无效的回调数据", show_alert=True)
              return None

          if self._prefix and not parts[0].startswith(self._prefix):
              await event.answer("无效的回调数据", show_alert=True)
              return None

          data["callback_parts"] = tuple(parts)
          return await handler(event, data)
  \`\`\`

  步骤 2: 在 app/bot/router.py 中为回调处理器创建验证器实例
  - session_callbacks = CallbackValidatorMiddleware(expected_parts=2, prefix="session:")
  - permission_callbacks = CallbackValidatorMiddleware(expected_parts=3, prefix="perm:")
  - user_question_callbacks = CallbackValidatorMiddleware(expected_parts=3, prefix="uq:")

  步骤 3: 应用到回调处理器并移除重复的验证代码
  目标处理器：
  - app/bot/handlers/session_actions.py 中的 5 个 callback handler
  - app/bot/handlers/external_permission.py 中的 2 个 callback handler
  - app/bot/handlers/command_user_question.py 中的 callback handler

  处理器中需要从 data 获取 callback_parts 而不是手动解析。

  执行创建和修改操作。
  `,
  { label: 'create-callback-validator', phase: '实现 CallbackValidatorMiddleware' }
)

// 阶段 4: 提取 PeriodicBackgroundTask 基类
phase('提取 PeriodicBackgroundTask 基类')

await agent(
  `创建 PeriodicBackgroundTask 基类并重构现有后台任务。

  步骤 1: 创建 app/infra/periodic_task.py
  \`\`\`python
  """周期性后台任务基类。"""
  from __future__ import annotations

  import asyncio
  import logging
  from abc import ABC, abstractmethod
  from contextlib import suppress
  from typing import Optional

  logger = logging.getLogger(__name__)


  class PeriodicBackgroundTask(ABC):
      """周期性后台任务基类。

      封装 asyncio.create_task + while True: sleep; work + cancel+suppress 的标准模式。

      Parameters
      ----------
      interval_seconds:
          任务执行间隔（秒）。
      task_name:
          任务名称，用于日志记录。
      """

      def __init__(
          self,
          interval_seconds: float,
          task_name: str = "PeriodicTask",
      ) -> None:
          self._interval = interval_seconds
          self._task_name = task_name
          self._task: Optional[asyncio.Task[None]] = None

      def start(self) -> None:
          """启动后台任务。"""
          if self._task is None or self._task.done():
              self._task = asyncio.create_task(self._periodic_loop())
              logger.info("%s started (interval=%.1fs)", self._task_name, self._interval)

      def stop(self) -> None:
          """停止后台任务。"""
          if self._task is not None and not self._task.done():
              self._task.cancel()
              self._task = None
              logger.info("%s stopped", self._task_name)

      @property
      def is_running(self) -> bool:
          """任务是否正在运行。"""
          return self._task is not None and not self._task.done()

      @abstractmethod
      async def _execute(self) -> None:
          """执行具体任务逻辑（子类实现）。"""
          ...

      def _on_error(self, exc: Exception) -> None:
          """错误处理钩子（子类可覆盖）。默认记录异常日志。"""
          logger.exception("%s error", self._task_name, exc_info=exc)

      async def _periodic_loop(self) -> None:
          """周期性执行循环。"""
          try:
              while True:
                  await asyncio.sleep(self._interval)
                  try:
                      await self._execute()
                  except Exception as exc:
                      self._on_error(exc)
          except asyncio.CancelledError:
              logger.info("%s cancelled", self._task_name)
  \`\`\`

  步骤 2: 创建 app/services/external_binding_cleanup_task.py
  \`\`\`python
  """外部绑定清理任务。"""
  from __future__ import annotations

  from app.infra.periodic_task import PeriodicBackgroundTask
  from app.services.external_binding_cleanup_service import ExternalBindingCleanupService


  class ExternalBindingCleanupTask(PeriodicBackgroundTask):
      """外部绑定清理任务。"""

      def __init__(
          self,
          cleanup_service: ExternalBindingCleanupService,
          interval_seconds: float = 60.0,
      ) -> None:
          super().__init__(interval_seconds, "ExternalBindingCleanup")
          self._cleanup_service = cleanup_service

      async def _execute(self) -> None:
          await self._cleanup_service.run_cleanup()
  \`\`\`

  步骤 3: 创建 app/services/janitor_task.py
  \`\`\`python
  """定期清理任务。"""
  from __future__ import annotations

  from app.infra.periodic_task import PeriodicBackgroundTask
  from app.services.periodic_janitor import PeriodicJanitor


  class JanitorTask(PeriodicBackgroundTask):
      """定期清理任务。"""

      def __init__(
          self,
          janitor: PeriodicJanitor,
          interval_seconds: float = 300.0,
      ) -> None:
          super().__init__(interval_seconds, "Janitor")
          self._janitor = janitor

      async def _execute(self) -> None:
          await self._janitor.run()
  \`\`\`

  步骤 4: 更新 app/bootstrap.py 使用新的任务类
  - 替换 ExternalBindingCleanupService 的 start/stop 调用
  - 替换 PeriodicJanitor 的 start/stop 调用

  执行创建和修改操作。
  `,
  { label: 'create-periodic-task', phase: '提取 PeriodicBackgroundTask 基类' }
)

// 阶段 5: 提取 SessionTombstoneStore
phase('提取 SessionTombstoneStore')

await agent(
  `创建 SessionTombstoneStore 统一墓碑管理。

  步骤 1: 创建 app/domain/session_tombstone.py
  \`\`\`python
  """会话墓碑存储。"""
  from __future__ import annotations

  import logging
  from datetime import datetime, timedelta
  from typing import Dict, Set

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
          self._ended: Dict[str, datetime] = {}
          self._unavailable: Dict[str, datetime] = {}
          self._ttl = timedelta(seconds=ttl_seconds)

      def mark_ended(self, session_id: str) -> None:
          """标记会话已结束。"""
          self._ended[session_id] = datetime.now()
          self._unavailable.pop(session_id, None)
          logger.debug("Marked session %s as ended", session_id)

      def mark_unavailable(self, session_id: str) -> None:
          """标记会话不可用。"""
          self._unavailable[session_id] = datetime.now()
          logger.debug("Marked session %s as unavailable", session_id)

      def is_ended(self, session_id: str) -> bool:
          """检查会话是否已结束。"""
          if session_id not in self._ended:
              return False
          # 检查是否过期
          if datetime.now() - self._ended[session_id] > self._ttl:
              del self._ended[session_id]
              return False
          return True

      def is_unavailable(self, session_id: str) -> bool:
          """检查会话是否不可用。"""
          if session_id not in self._unavailable:
              return False
          # 检查是否过期
          if datetime.now() - self._unavailable[session_id] > self._ttl:
              del self._unavailable[session_id]
              return False
          return True

      def ended_ids(self) -> Set[str]:
          """获取所有已结束的会话 ID（自动清理过期记录）。"""
          self._cleanup_expired_ended()
          return set(self._ended.keys())

      def unavailable_ids(self) -> Set[str]:
          """获取所有不可用的会话 ID（自动清理过期记录）。"""
          self._cleanup_expired_unavailable()
          return set(self._unavailable.keys())

      def cleanup_expired(self) -> None:
          """清理所有过期的墓碑记录。"""
          self._cleanup_expired_ended()
          self._cleanup_expired_unavailable()

      def _cleanup_expired_ended(self) -> None:
          now = datetime.now()
          expired = [k for k, v in self._ended.items() if now - v > self._ttl]
          for k in expired:
              del self._ended[k]
              logger.debug("Cleaned up expired ended tombstone: %s", k)

      def _cleanup_expired_unavailable(self) -> None:
          now = datetime.now()
          expired = [k for k, v in self._unavailable.items() if now - v > self._ttl]
          for k in expired:
              del self._unavailable[k]
              logger.debug("Cleaned up expired unavailable tombstone: %s", k)
  \`\`\`

  步骤 2: 更新 app/services/external_session_discovery.py
  - 将 _ended_session_ids 和 _unavailable_session_ids 替换为 SessionTombstoneStore
  - 更新 is_session_ended 方法使用 tombstone store
  - 更新 mark_session_ended 和 mark_session_unavailable 方法

  步骤 3: 更新 app/services/auto_approve_service.py
  - 将 _ended_sessions 替换为 SessionTombstoneStore
  - 更新 is_session_ended 方法

  步骤 4: 更新 app/services/external_binding_reaper.py
  - 将 _ended_sessions 替换为 SessionTombstoneStore
  - 更新相关方法

  步骤 5: 在 app/bootstrap.py 中创建共享的 SessionTombstoneStore 实例
  - 注入到需要的服务中

  执行创建和修改操作。
  `,
  { label: 'create-tombstone-store', phase: '提取 SessionTombstoneStore' }
)

// 阶段 6: 验证测试
phase('验证测试')

await agent(
  `运行测试套件验证所有重构是否正确：

  1. 运行所有测试：python -m pytest tests/ -v
  2. 检查是否有导入错误
  3. 检查是否有类型错误

  如果测试失败，分析原因并修复。

  报告测试结果。
  `,
  { label: 'run-tests', phase: '验证测试' }
)

await agent(
  `进行代码质量检查：

  1. 检查是否有未使用的导入
  2. 检查是否有重复代码
  3. 检查命名规范
  4. 运行 mypy 检查类型注解

  报告检查结果。
  `,
  { label: 'quality-check', phase: '验证测试' }
)

return { status: 'Phase 2 refactoring completed' }
