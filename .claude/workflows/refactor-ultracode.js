export const meta = {
  name: 'refactor-ultracode',
  description: 'ultracode 代码重构：删除死代码、提取重复函数、实现中间件',
  phases: [
    { title: '删除死代码' },
    { title: '提取重复函数' },
    { title: '实现中间件' },
    { title: '验证测试' },
  ],
}

// 阶段 1: 删除死代码
phase('删除死代码')

// 1.1 删除死代码文件
await agent(
  `删除以下死代码文件：
  1. app/services/agent_file_watcher.py - 已被 SessionSupervisor 替代
  2. app/services/interrupt_watcher.py - 已被 SessionSupervisor 替代

  同时清理所有引用这些文件的导入语句。

  执行删除操作并报告结果。
  `,
  { label: 'delete-dead-files', phase: '删除死代码' }
)

// 1.2 清理未使用的函数和类
await agent(
  `清理以下未使用的函数和类：
  1. app/adapters/process/pty_injector.py 中的 inject_text_answer 函数（约第 140 行）
  2. app/adapters/storage/memory.py 中的 MemorySessionStore 类（约第 84 行）

  执行删除操作并报告结果。
  `,
  { label: 'delete-unused-funcs', phase: '删除死代码' }
)

// 阶段 2: 提取重复函数
phase('提取重复函数')

// 2.1 提取 file_mtime_utils 工具模块
await agent(
  `创建 app/infra/file_mtime_utils.py 工具模块，提取以下重复函数：

  从 app/services/session_supervisor.py 提取：
  - _refresh_seen_mtimes 函数
  - _clear_seen_mtimes 函数

  原始实现参考 agent_file_watcher.py 中的同名函数（已被删除，但逻辑相同）。

  创建的工具函数应：
  1. 使用 async def
  2. 接收 paths: Set[str], seen_mtimes: Dict[str, float] 参数
  3. 返回更新的 mtime 字典
  4. 包含完整的类型注解和文档字符串

  然后更新 session_supervisor.py 使用新的工具函数。

  示例实现：
  \`\`\`python
  # app/infra/file_mtime_utils.py
  from typing import Dict, Set
  import os
  import logging

  logger = logging.getLogger(__name__)

  async def refresh_seen_mtimes(
      paths: Set[str],
      seen_mtimes: Dict[str, float]
  ) -> Dict[str, float]:
      \"\"\"刷新文件修改时间缓存\"\"\"
      updated = {}
      for path in paths:
          try:
              mtime = os.path.getmtime(path)
              if path not in seen_mtimes or seen_mtimes[path] != mtime:
                  updated[path] = mtime
          except OSError:
              continue
      seen_mtimes.update(updated)
      return updated

  async def clear_seen_mtimes_for_session(
      session_id: str,
      seen_mtimes: Dict[str, float]
  ) -> None:
      \"\"\"清除指定会话的文件修改时间缓存\"\"\"
      keys_to_remove = [k for k in seen_mtimes if session_id in k]
      for key in keys_to_remove:
          del seen_mtimes[key]
  \`\`\`

  执行创建和更新操作。
  `,
  { label: 'extract-file-mtime-utils', phase: '提取重复函数' }
)

// 2.2 提取 CLI 适配器基类方法
await agent(
  `分析 app/adapters/cli/codex_cli.py 和 app/adapters/cli/gemini_cli.py 中的 run 函数。

  这两个函数相似度高达 99.3%，需要提取到基类 app/adapters/cli/base.py。

  步骤：
  1. 读取两个文件的 run 函数实现
  2. 找出共同逻辑
  3. 在 base.py 中创建基类方法
  4. 更新两个子类使用基类方法

  执行提取操作。
  `,
  { label: 'extract-cli-base-run', phase: '提取重复函数' }
)

// 2.3 提取 _load_gitignore_patterns 工具函数
await agent(
  `提取 _load_gitignore_patterns 函数到 app/infra/gitignore_utils.py。

  这个函数在以下两个文件中重复：
  - app/bot/handlers/run_event_streamer.py
  - app/services/result_exporter.py

  创建工具模块并更新两个文件使用新函数。

  示例实现：
  \`\`\`python
  # app/infra/gitignore_utils.py
  from pathlib import Path
  from typing import List, Optional
  import pathspec

  def load_gitignore_patterns(
      directory: Path,
      filename: str = '.gitignore'
  ) -> Optional[pathspec.PathSpec]:
      \"\"\"加载 .gitignore 模式\"\"\"
      gitignore_path = directory / filename
      if not gitignore_path.exists():
          return None

      with open(gitignore_path) as f:
          patterns = f.read().splitlines()

      return pathspec.PathSpec.from_lines('gitwild', patterns)
  \`\`\`

  执行创建和更新操作。
  `,
  { label: 'extract-gitignore-utils', phase: '提取重复函数' }
)

// 2.4 统一 bind/unbind 处理器
await agent(
  `重构 app/bot/handlers/session_actions.py 中的 handle_session_bind 和 handle_session_unbind 函数。

  这两个函数相似度 97.8%，需要提取通用处理器。

  步骤：
  1. 创建 _handle_bind_unbind_action(callback, action_type: str) 通用函数
  2. 重构 handle_session_bind 和 handle_session_unbind 调用通用函数
  3. 同样处理 app/bot/handlers/external_session.py 中的 _handle_bind 和 _handle_unbind

  执行重构操作。
  `,
  { label: 'unify-bind-unbind', phase: '提取重复函数' }
)

// 2.5 提取通用媒体发送方法
await agent(
  `重构 app/services/message_sender.py 和 app/bot/adapters/message_sender.py 中的重复方法。

  相似函数：
  - send_photo 和 send_document（相似度 95.2%）
  - send_message 和 edit_message（相似度 90.9%）

  提取通用方法：
  \`\`\`python
  async def _send_media(
      self,
      chat_id: int,
      media: Any,
      media_type: str,
      caption: Optional[str] = None,
      reply_to_message_id: Optional[int] = None
  ) -> Message:
      \"\"\"通用媒体发送方法\"\"\"
      ...

  async def _send_or_edit_message(
      self,
      chat_id: int,
      text: str,
      message_id: Optional[int] = None,
      parse_mode: str = 'HTML'
  ) -> Message:
      \"\"\"通用消息发送/编辑方法\"\"\"
      ...
  \`\`\`

  执行重构操作。
  `,
  { label: 'extract-media-sender', phase: '提取重复函数' }
)

// 阶段 3: 实现中间件
phase('实现中间件')

// 3.1 实现错误处理中间件
await agent(
  `创建 app/bot/middleware/error_handling.py 错误处理中间件。

  实现：
  \`\`\`python
  from typing import Callable, Any
  import logging
  from aiogram import BaseMiddleware
  from aiogram.types import Message, CallbackQuery

  logger = logging.getLogger(__name__)

  class ErrorHandlingMiddleware(BaseMiddleware):
      \"\"\"统一错误处理中间件\"\"\"

      async def __call__(
          self,
          handler: Callable,
          event: Message | CallbackQuery,
          data: dict
      ) -> Any:
          try:
              return await handler(event, data)
          except ValueError as exc:
              logger.warning(f"Handler error: {exc}")
              if isinstance(event, Message):
                  await event.answer(f"操作失败: {exc}")
              elif isinstance(event, CallbackQuery):
                  await event.answer(f"操作失败: {exc}", show_alert=True)
          except Exception as exc:
              logger.exception(f"Handler exception")
              if isinstance(event, Message):
                  await event.answer("发生内部错误，请稍后重试")
              elif isinstance(event, CallbackQuery):
                  await event.answer("发生内部错误", show_alert=True)
  \`\`\`

  然后在 app/bot/router.py 中注册中间件。

  执行创建和注册操作。
  `,
  { label: 'create-error-middleware', phase: '实现中间件' }
)

// 3.2 实现会话守卫中间件
await agent(
  `创建 app/bot/middleware/session_guard.py 会话守卫中间件。

  实现：
  \`\`\`python
  from typing import Callable, Any, Optional
  from aiogram import BaseMiddleware
  from aiogram.types import Message, CallbackQuery

  class SessionGuardMiddleware(BaseMiddleware):
      \"\"\"会话守卫中间件\"\"\"

      def __init__(self, session_service, require_active: bool = False):
          self._session_service = session_service
          self._require_active = require_active

      async def __call__(
          self,
          handler: Callable,
          event: Message | CallbackQuery,
          data: dict
      ) -> Any:
          user_id = data.get('user_id')
          if not user_id:
              return await handler(event, data)

          session = await self._session_service.get(user_id)

          if session is None:
              error_msg = "请先使用 /session 或 /claude 创建会话"
              if isinstance(event, Message):
                  await event.answer(error_msg)
              elif isinstance(event, CallbackQuery):
                  await event.answer(error_msg, show_alert=True)
              return

          if self._require_active and not session.claude_chat_active:
              error_msg = "请先发送 /claude 开启会话"
              if isinstance(event, Message):
                  await event.answer(error_msg)
              elif isinstance(event, CallbackQuery):
                  await event.answer(error_msg, show_alert=True)
              return

          data['session'] = session
          return await handler(event, data)
  \`\`\`

  然后在需要的处理器上应用中间件。

  执行创建和应用操作。
  `,
  { label: 'create-session-guard', phase: '实现中间件' }
)

// 3.3 实现回调数据验证中间件
await agent(
  `创建 app/bot/middleware/callback_validator.py 回调数据验证中间件。

  实现：
  \`\`\`python
  from typing import Callable, Any, Optional, Tuple
  from aiogram import BaseMiddleware
  from aiogram.types import CallbackQuery

  class CallbackValidatorMiddleware(BaseMiddleware):
      \"\"\"回调数据验证中间件\"\"\"

      def __init__(
          self,
          expected_parts: int,
          prefix: Optional[str] = None
      ):
          self._expected_parts = expected_parts
          self._prefix = prefix

      async def __call__(
          self,
          handler: Callable,
          event: CallbackQuery,
          data: dict
      ) -> Any:
          if not event.data:
              await event.answer("无效的回调数据", show_alert=True)
              return

          parts = event.data.split(':')

          if len(parts) != self._expected_parts:
              await event.answer("无效的回调数据", show_alert=True)
              return

          if self._prefix and not parts[0].startswith(self._prefix):
              await event.answer("无效的回调数据", show_alert=True)
              return

          data['callback_parts'] = tuple(parts)
          return await handler(event, data)
  \`\`\`

  然后在回调处理器上应用中间件。

  执行创建和应用操作。
  `,
  { label: 'create-callback-validator', phase: '实现中间件' }
)

// 3.4 提取通用工具函数
await agent(
  `创建 app/bot/handlers/callback_utils.py 中的通用工具函数。

  添加以下函数：
  \`\`\`python
  from typing import Optional, Tuple, Any
  import logging

  logger = logging.getLogger(__name__)

  def parse_callback_prefix(
      data: str,
      expected_parts: int,
      prefix: str
  ) -> Optional[Tuple[str, ...]]:
      \"\"\"解析 callback data 前缀\"\"\"
      parts = data.split(':')
      if len(parts) != expected_parts:
          return None
      if not parts[0].startswith(prefix):
          return None
      return tuple(parts)

  async def safe_edit_keyboard(
      message: Any,
      keyboard: Optional[Any],
      log_prefix: str
  ) -> bool:
      \"\"\"安全地编辑键盘\"\"\"
      try:
          await message.edit_reply_markup(reply_markup=keyboard)
          return True
      except Exception:
          logger.exception(f"{log_prefix} failed")
          return False
  \`\`\`

  然后更新使用这些模式的处理器。

  执行添加和更新操作。
  `,
  { label: 'extract-callback-utils', phase: '实现中间件' }
)

// 阶段 4: 验证测试
phase('验证测试')

// 4.1 运行测试套件
await agent(
  `运行测试套件验证重构是否正确：

  1. 运行所有测试：python -m pytest tests/ -v
  2. 检查是否有导入错误
  3. 检查是否有类型错误

  如果测试失败，分析原因并修复。

  报告测试结果。
  `,
  { label: 'run-tests', phase: '验证测试' }
)

// 4.2 代码质量检查
await agent(
  `进行代码质量检查：

  1. 检查是否有未使用的导入
  2. 检查是否有重复代码
  3. 检查命名规范

  使用 flake8 或 ruff 进行静态分析。

  报告检查结果。
  `,
  { label: 'quality-check', phase: '验证测试' }
)

return { status: 'Refactoring completed' }
