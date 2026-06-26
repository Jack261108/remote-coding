# Remote-Coding 模块调用关系图

## 1. 架构分层概览

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              Telegram Bot 层                                    │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐                │
│  │   middleware/    │  │   handlers/     │  │   presenters/   │                │
│  │  AuthMiddleware  │  │   command_*.py  │  │  Structured     │                │
│  │  RateLimit      │  │   file_upload   │  │  ReplyPresenter │                │
│  └────────┬────────┘  └────────┬────────┘  └────────┬────────┘                │
└───────────┼────────────────────┼────────────────────┼──────────────────────────┘
            │                    │                    │
            ▼                    ▼                    ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                             服务层 (services/)                                   │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │                        TaskService (核心协调器)                          │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘
            │
            ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           适配器层 (adapters/)                                   │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐          │
│  │ cli/        │  │ process/    │  │ storage/    │  │ claude/     │          │
│  │ ClaudeCode  │  │ Subprocess  │  │ Memory      │  │ HookSocket  │          │
│  │ CodexCLI    │  │ TmuxRunner  │  │ FileSession │  │ Server      │          │
│  │ GeminiCLI   │  │             │  │ UploadStore │  │             │          │
│  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘          │
└─────────────────────────────────────────────────────────────────────────────────┘
            │
            ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                          领域模型层 (domain/)                                    │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐          │
│  │ models.py   │  │ session     │  │ hook        │  │ permission  │          │
│  │ CLIEvent    │  │ _models.py  │  │ _models.py  │  │ _models.py  │          │
│  │ TaskRecord  │  │ SessionState│  │ HookEvent   │  │ Permission  │          │
│  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘          │
└─────────────────────────────────────────────────────────────────────────────────┘
            │
            ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         基础设施层 (infra/ + config/)                            │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐          │
│  │ logging     │  │ async_utils │  │ text_format │  │ settings    │          │
│  │ formatter   │  │ locks       │  │ting         │  │ .env        │          │
│  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘          │
└─────────────────────────────────────────────────────────────────────────────────┘
```

## 2. Bot Handler → Service 调用映射

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              Bot Handlers                                       │
└─────────────────────────────────────────────────────────────────────────────────┘
            │
            │ command_run
            ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                     command_run.py                                              │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  run_prompt_and_stream()                                               │   │
│  │    │                                                                   │   │
│  │    ├─► TaskService.create_and_run(user_id, provider, prompt, workdir)  │   │
│  │    │                                                                   │   │
│  │    ├─► RunEventStreamer.stream_events(cli_events)                      │   │
│  │    │      │                                                            │   │
│  │    │      ├─► StatusDisplayService.update_for_tool(tool_name)          │   │
│  │    │      ├─► StructuredReplyPresenter.poll()                          │   │
│  │    │      └─► DiffGeneratorService.capture_snapshot()                  │   │
│  │    │                                                                   │   │
│  │    └─► TaskService.mark_stream_timeout() / cancel()                   │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────────┐
│                     command_claude.py                                           │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  handle_claude()                                                       │   │
│  │    ├─► TaskService.is_workdir_allowed(workdir)                         │   │
│  │    └─► TaskService.open_claude_chat_session(user_id, workdir)          │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────────┐
│                     command_resume.py                                           │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  handle_resume()                                                       │   │
│  │    ├─► SessionService.get(user_id)                                     │   │
│  │    └─► TaskService.open_claude_resume_session(user_id, session_id)     │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────────┐
│                     command_status.py                                           │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  handle_status()                                                       │   │
│  │    ├─► TaskService.get_status(task_id, user_id)                        │   │
│  │    ├─► TaskService.get_structured_session_for_task(task_id, user_id)   │   │
│  │    └─► TaskService.list_recent(user_id, limit)                         │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────────┐
│                     command_permission.py                                       │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  handle_approve() / handle_deny() / handle_callback()                  │   │
│  │    ├─► PermissionGateway.handle_approve_command(user_id)               │   │
│  │    ├─► PermissionGateway.handle_deny_command(user_id, reason)          │   │
│  │    └─► PermissionGateway.handle_callback(data, user_id)                │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────────┐
│                     command_session.py                                          │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  handle_session()                                                      │   │
│  │    ├─► SessionService.get(user_id)                                     │   │
│  │    ├─► SessionService.switch(user_id, provider, workdir)               │   │
│  │    └─► TaskService.get_structured_session(user_id)                     │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────────┐
│                     command_user_question.py                                    │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  handle_user_question()                                                │   │
│  │    ├─► TaskService.get_pending_user_questions(user_id)                 │   │
│  │    ├─► TaskService.answer_pending_user_question_option(...)            │   │
│  │    ├─► TaskService.answer_pending_user_question_text(...)              │   │
│  │    ├─► TaskService.toggle_pending_user_question_multi_select(...)      │   │
│  │    ├─► TaskService.submit_pending_user_question_multi_select(...)      │   │
│  │    └─► TaskService.acknowledge_structured_user_question(...)           │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────────┐
│                     command_list.py                                             │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  handle_list()                                                         │   │
│  │    ├─► SessionRegistryService.list_active_sessions()                   │   │
│  │    └─► ExternalSessionBinder.list_bound_for_user(user_id)              │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────────┐
│                     command_attach.py                                           │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  handle_attach() / handle_detach()                                     │   │
│  │    ├─► SessionRegistryService.attach_user(user_id, terminal_id)       │   │
│  │    └─► SessionRegistryService.detach_user(user_id)                     │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────────┐
│                     command_cancel.py                                           │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  handle_cancel()                                                       │   │
│  │    └─► TaskService.cancel(task_id, user_id)                            │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────────┐
│                     command_exit.py                                             │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  handle_exit()                                                         │   │
│  │    └─► TaskService.close_terminal(user_id)                             │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────────┐
│                     command_export.py                                           │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  handle_export()                                                       │   │
│  │    ├─► TaskService.get_status(task_id, user_id)                        │   │
│  │    └─► ResultExporterService.export_zip(task_id, user_id)              │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────────┐
│                     file_upload.py                                              │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  handle_document() / handle_photo()                                    │   │
│  │    ├─► FileReceiverService.receive_file(file)                          │   │
│  │    ├─► SessionService.get(user_id)                                     │   │
│  │    └─► TaskService.list_recent(user_id, limit)                         │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘
```

## 3. TaskService 内部调用链

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           TaskService                                           │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │                                                                         │   │
│  │  create_and_run(user_id, provider, prompt, workdir)                    │   │
│  │    │                                                                   │   │
│  │    ├─► ContextBuilderService.build_context(uploads)                    │   │
│  │    │                                                                   │   │
│  │    ├─► CLIAdapterFactory.get(provider)                                │   │
│  │    │      │                                                            │   │
│  │    │      ├─► ClaudeCodeAdapter                                       │   │
│  │    │      ├─► CodexCLIAdapter                                         │   │
│  │    │      └─► GeminiCLIAdapter                                        │   │
│  │    │                                                                   │   │
│  │    ├─► adapter.run(execution_task) -> AsyncIterator[CLIEvent]          │   │
│  │    │                                                                   │   │
│  │    └─► MemoryTaskStore.save(TaskRecord)                               │   │
│  │                                                                         │   │
│  │  cancel(task_id, user_id)                                              │   │
│  │    ├─► MemoryTaskStore.get(task_id)                                    │   │
│  │    └─► adapter.cancel(task_id)                                         │   │
│  │                                                                         │   │
│  │  get_status(task_id, user_id)                                          │   │
│  │    └─► MemoryTaskStore.get(task_id)                                    │   │
│  │                                                                         │   │
│  │  get_structured_session(user_id)                                       │   │
│  │    ├─► SessionService.get(user_id)                                     │   │
│  │    └─► StructuredSessionResolver.resolve(user_id)                      │   │
│  │                                                                         │   │
│  │  open_claude_chat_session(user_id, workdir)                            │   │
│  │    ├─► CLIAdapterFactory.ensure_terminal(user_id, workdir)            │   │
│  │    └─► SessionService.get_or_create(user_id, provider, workdir)       │   │
│  │                                                                         │   │
│  │  respond_to_pending_permission(tool_use_id, decision, reason)          │   │
│  │    └─► PermissionService.respond_to_pending_permission(...)            │   │
│  │            │                                                           │   │
│  │            └─► HookSocketServer.respond_to_permission(...)             │   │
│  │                                                                         │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘
```

## 4. PermissionGateway 内部调用链

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                        PermissionGateway                                        │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │                                                                         │   │
│  │  maybe_auto_approve(session_id, origin, user_id, tool_use_id,          │   │
│  │                      tool_name, tool_input)                             │   │
│  │    │                                                                   │   │
│  │    ├─► RiskEvaluator.evaluate(tool_name, tool_input)                   │   │
│  │    │      │                                                            │   │
│  │    │      └─► RiskAssessment { risk_level, should_block }              │   │
│  │    │                                                                   │   │
│  │    └─► AutoApproveService.maybe_auto_approve(...)                      │   │
│  │           │                                                            │   │
│  │           ├─► is_active(session_id, user_id)                           │   │
│  │           └─► Slot management (claim/release)                          │   │
│  │                                                                         │   │
│  │  register_for_button(tool_use_id, session_id, origin, user_id)         │   │
│  │    ├─► PermissionCallbackRegistry.register_token(...)                  │   │
│  │    │      └─► Returns RegisterForButtonOk { token, keyboard }         │   │
│  │    └─► PermissionMessageBuilder.build_keyboard(...)                    │   │
│  │                                                                         │   │
│  │  handle_callback(data, user_id)                                        │   │
│  │    ├─► PermissionCallbackRegistry.consume(token, user_id, action)      │   │
│  │    │      └─► Returns ToolUsePermissionRequest                         │   │
│  │    └─► HookSocketServer.respond_to_permission(tool_use_id, decision)  │   │
│  │                                                                         │   │
│  │  handle_approve_command(user_id)                                       │   │
│  │    └─► PendingPermissionRegistry.get_pending(user_id)                  │   │
│  │                                                                         │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘
```

## 5. SessionStore 内部调用链

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           SessionStore                                          │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │                                                                         │   │
│  │  process(event) -> SessionState                                        │   │
│  │    ├─► SessionEventProcessor.apply_event(state, event)                │   │
│  │    │      └─► Returns new SessionState                                 │   │
│  │    │                                                                   │   │
│  │    ├─► SessionStateCache.invalidate(session_id)                       │   │
│  │    │                                                                   │   │
│  │    ├─► SessionStateRepository.save(state)                              │   │
│  │    │      └─► FileSessionStore.save(state)                             │   │
│  │    │                                                                   │   │
│  │    └─► SessionNotifier.publish(state)                                  │   │
│  │           └─► notify waiters (wait_for_publish, wait_for_change)       │   │
│  │                                                                         │   │
│  │  get(session_id) -> SessionState | None                                │   │
│  │    ├─► SessionStateCache.get(session_id)                              │   │
│  │    │                                                                   │   │
│  │    └─► [cache miss] FileSessionStore.get(session_id)                  │   │
│  │                                                                         │   │
│  │  wait_for_publish(session_id, since_cursor, timeout)                   │   │
│  │    └─► SessionNotifier.wait(session_id, condition, timeout)            │   │
│  │                                                                         │   │
│  │  find_by_pending_tool_use_id(tool_use_id) -> SessionState | None       │   │
│  │    └─► SessionLookupService.find_by_pending_tool_use_id(...)           │   │
│  │                                                                         │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘
```

## 6. CLI Adapter 调用链

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         CLIAdapterFactory                                       │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │                                                                         │   │
│  │  get(provider) -> BaseCLIAdapter                                       │   │
│  │    ├─► ClaudeCodeAdapter(settings, tmux_runner)                        │   │
│  │    ├─► CodexCLIAdapter(settings, subprocess_runner)                    │   │
│  │    └─► GeminiCLIAdapter(settings, subprocess_runner)                   │   │
│  │                                                                         │   │
│  │  ensure_terminal(user_id, workdir) -> str                              │   │
│  │    └─► TmuxRunner.ensure_terminal(user_id, workdir)                    │   │
│  │                                                                         │   │
│  │  ensure_claude_interactive_session(user_id, workdir) -> str            │   │
│  │    └─► TmuxRunner.ensure_claude_interactive_session(...)               │   │
│  │                                                                         │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘
            │
            ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                      ClaudeCodeAdapter                                          │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  run(execution_task) -> AsyncIterator[CLIEvent]                        │   │
│  │    ├─► TmuxRunner.ensure_claude_resume_session(...)                    │   │
│  │    │                                                                   │   │
│  │    └─► yield from TmuxRunner (via yield_terminal_events)              │   │
│  │                                                                         │   │
│  │  cancel(task_id) -> bool                                               │   │
│  │    └─► TmuxRunner.send_interactive_input("/cancel")                   │   │
│  │                                                                         │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘
            │
            ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                          TmuxRunner                                             │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  ensure_terminal(user_id, workdir) -> str                              │   │
│  │    ├─► tmux has-session check                                          │   │
│  │    └─► tmux new-session / switch-client                                 │   │
│  │                                                                         │   │
│  │  ensure_claude_resume_session(user_id, session_id) -> str              │   │
│  │    ├─► tmux send-keys "claude --resume ..."                            │   │
│  │    └─► FileSessionStore.create_terminal_session(...)                   │   │
│  │                                                                         │   │
│  │  send_interactive_input(text)                                          │   │
│  │    └─► tmux send-keys                                                  │   │
│  │                                                                         │   │
│  │  get_session_state(terminal_id) -> SessionState                        │   │
│  │    └─► FileSessionStore.get(terminal_id)                               │   │
│  │                                                                         │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘
            │
            ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                       HookSocketServer                                          │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │  start()                                                               │   │
│  │    └─► asyncio.start_unix_server(_handle_client)                       │   │
│  │                                                                         │   │
│  │  _handle_client(reader, writer)                                        │   │
│  │    ├─► Parse JSON from socket                                           │   │
│  │    ├─► HookEvent.from_dict(data)                                       │   │
│  │    └─► emit("hook_event", event)                                       │   │
│  │                                                                         │   │
│  │  respond_to_permission(tool_use_id, decision)                          │   │
│  │    ├─► PendingPermissionRequest lookup                                 │   │
│  │    └─► writer.write(HookResponse(...).to_dict())                       │   │
│  │                                                                         │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘
```

## 7. StatusDisplayService 调用链

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                       StatusDisplayService                                       │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │                                                                         │   │
│  │  start(task_id, chat_id)                                               │   │
│  │    └─► transition(to_phase=STARTING)                                   │   │
│  │         └─► bot.send_chat_action(chat_id, ChatAction.TYPING)           │   │
│  │                                                                         │   │
│  │  update_for_tool(task_id, chat_id, tool_name)                          │   │
│  │    ├─► TOOL_PHASE_MAP.get(tool_name) -> TaskPhase                     │   │
│  │    └─► transition(to_phase=phase)                                      │   │
│  │         └─► bot.send_chat_action(chat_id, action)                      │   │
│  │                                                                         │   │
│  │  complete(task_id, chat_id)                                            │   │
│  │    └─► transition(to_phase=COMPLETED)                                  │   │
│  │         └─► chat_id in _active_sessions -> del                        │   │
│  │                                                                         │   │
│  │  fail(task_id, chat_id)                                                │   │
│  │    └─► transition(to_phase=FAILED)                                     │   │
│  │         └─► chat_id in _active_sessions -> del                        │   │
│  │                                                                         │   │
│  │  transition(task_id, chat_id, to_phase)                                │   │
│  │    ├─► _validate_transition(current, to_phase)                        │   │
│  │    │      └─► TRANSITIONS[current].get(to_phase)                       │   │
│  │    │                                                                   │   │
│  │    └─► _send_action(chat_id, action)                                   │   │
│  │           └─► bot.send_chat_action(chat_id, action)                    │   │
│  │                                                                         │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘
```

## 8. RunEventStreamer 事件处理流

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         RunEventStreamer                                         │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │                                                                         │   │
│  │  stream_events(cli_events, task, chat_id, message_id)                  │   │
│  │    │                                                                   │   │
│  │    │   for event in cli_events:                                        │   │
│  │    │                                                                   │   │
│  │    │   ┌─────────────────────────────────────────────────────────────┐ │   │
│  │    │   │ EventType.STARTED                                          │ │   │
│  │    │   │   └─► StatusDisplayService.start(task_id, chat_id)        │ │   │
│  │    │   └─────────────────────────────────────────────────────────────┘ │   │
│  │    │                                                                   │   │
│  │    │   ┌─────────────────────────────────────────────────────────────┐ │   │
│  │    │   │ EventType.TEXT / EventType.TOOL_STATE                      │ │   │
│  │    │   │   ├─► StatusDisplayService.update_for_tool(tool_name)     │ │   │
│  │    │   │   ├─► StructuredReplyPresenter.poll()                      │ │   │
│  │    │   │   └─► PresenterOutputDispatcher.emit_presenter_messages() │ │   │
│  │    │   │         │                                                   │ │   │
│  │    │   │         ├─► PermissionRequestOutput                        │ │   │
│  │    │   │         │     ├─► PermissionGateway.maybe_auto_approve()   │ │   │
│  │    │   │         │     └─► PermissionGateway.register_for_button()  │ │   │
│  │    │   │         │                                                   │ │   │
│  │    │   │         ├─► StructuredReplyOutput                          │ │   │
│  │    │   │         │     └─► ChunkSender.send()                       │ │   │
│  │    │   │         │           └─► RunTelegramMessenger.send()        │ │   │
│  │    │   │         │                                                   │ │   │
│  │    │   │         └─► ToolStatusOutput                               │ │   │
│  │    │   │               └─► ToolMessageManager.handle()              │ │   │
│  │    │   └─────────────────────────────────────────────────────────────┘ │   │
│  │    │                                                                   │   │
│  │    │   ┌─────────────────────────────────────────────────────────────┐ │   │
│  │    │   │ EventType.COMPLETED / EventType.FAILED                     │ │   │
│  │    │   │   ├─► StatusDisplayService.complete/fail(task_id, chat_id)│ │   │
│  │    │   │   └─► ResultExporterService.should_auto_export()          │ │   │
│  │    │   │         └─► export_zip() / export_markdown()               │ │   │
│  │    │   └─────────────────────────────────────────────────────────────┘ │   │
│  │    │                                                                   │   │
│  │    └─► TaskService._apply_event(event) -> MemoryTaskStore            │   │
│  │                                                                         │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘
```

## 9. 完整依赖关系矩阵

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              依赖关系矩阵                                        │
├──────────────────────┬──────────────────────────────────────────────────────────┤
│ 模块                  │ 依赖模块                                                │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ domain/*             │ domain.models (utc_now)                                  │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ infra/*              │ 无内部依赖                                               │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ config/*             │ 无内部依赖                                               │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ adapters/claude/*    │ config.settings, domain.hook_models, domain.models      │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ adapters/cli/*       │ adapters.process, config.settings, domain.*             │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ adapters/process/*   │ adapters.storage.file_session_store, domain.*, infra.*  │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ adapters/storage/*   │ domain.*, domain.hook_models                            │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ services/task_service│ adapters.cli.factory, adapters.storage.memory,          │
│                      │ config.settings, domain.*, services.auto_approve,       │
│                      │ services.permission_service, services.session_service,  │
│                      │ services.session_store, services.structured_session,    │
│                      │ services.task_lifecycle, services.terminal_session,     │
│                      │ services.user_question                                  │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ services/permission  │ services.auto_approve, services.message_sender,         │
│ _gateway             │ services.permission_callback_registry,                  │
│                      │ services.risk_evaluator                                 │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ services/session_store│ adapters.storage.file_session_store, domain.*,         │
│                      │ services.session_event_processor,                       │
│                      │ services.session_lookup, services.session_notifier,     │
│                      │ services.session_state_cache,                           │
│                      │ services.session_state_repository,                      │
│                      │ services.structured_reply_tracker                       │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ bot/handlers/*       │ bot.presenters.*, domain.*, services.*                  │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ bot/presenters/*     │ domain.session_models, infra.text_formatting,           │
│                      │ services.task_service                                   │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ bot/router           │ 所有 handler 注册函数, bot.presenters.chunk_sender,     │
│                      │ config.settings, services.*                             │
├──────────────────────┼──────────────────────────────────────────────────────────┤
│ bootstrap            │ 所有模块 -- 组合根                                      │
└──────────────────────┴──────────────────────────────────────────────────────────┘
```

## 10. 数据流向图

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              数据流向图                                           │
└─────────────────────────────────────────────────────────────────────────────────┘

用户输入                     Claude Code 输出                 Telegram 消息
    │                              │                              │
    ▼                              ▼                              ▼
┌─────────┐                  ┌─────────┐                  ┌─────────┐
│ Telegram │                  │ JSONL   │                  │ Telegram │
│ Message  │                  │ Log     │                  │ Bot API  │
└────┬────┘                  └────┬────┘                  └────▲────┘
     │                             │                            │
     ▼                             ▼                            │
┌─────────┐                  ┌─────────┐                  ┌─────────┐
│ Bot     │                  │ Session │                  │ Chunk   │
│ Handler │                  │ Parser  │                  │ Sender  │
└────┬────┘                  └────┬────┘                  └────▲────┘
     │                             │                            │
     ▼                             ▼                            │
┌─────────┐                  ┌─────────┐                  ┌─────────┐
│ Task    │                  │Structured│                  │ Message │
│ Service │                  │Reply    │                  │ Builder │
└────┬────┘                  └────┬────┘                  └────▲────┘
     │                             │                            │
     ▼                             ▼                            │
┌─────────┐                  ┌─────────┐                  ┌─────────┐
│ CLI     │                  │Event    │                  │ Format  │
│ Adapter │                  │Streamer │                  │ Converter│
└────┬────┘                  └─────────┘                  └─────────┘
     │
     ▼
┌─────────┐
│ Claude  │
│ Code    │
└─────────┘
```

## 11. 权限审批完整流程

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         权限审批完整流程                                         │
└─────────────────────────────────────────────────────────────────────────────────┘

┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│ Claude Code      │     │ HookSocketServer │     │ PermissionGateway│
│ Permission Request│────►│ receive(event)   │────►│ maybe_auto_approve│
└──────────────────┘     └──────────────────┘     └────────┬─────────┘
                                                          │
                                            ┌──────────────┴──────────────┐
                                            │                             │
                                            ▼                             ▼
                                   ┌──────────────────┐     ┌──────────────────┐
                                   │ RiskEvaluator    │     │ AutoApproveService│
                                   │ evaluate()       │     │ is_active()       │
                                   └────────┬─────────┘     └────────┬─────────┘
                                            │                             │
                                            ▼                             ▼
                                   ┌──────────────────┐     ┌──────────────────┐
                                   │ RiskAssessment   │     │ Slot Management  │
                                   │ should_block     │     │ claim/release    │
                                   └────────┬─────────┘     └────────┬─────────┘
                                            │                             │
                                            └──────────────┬──────────────┘
                                                          │
                                            ┌──────────────┴──────────────┐
                                            │                             │
                                            ▼                             ▼
                                   ┌──────────────────┐     ┌──────────────────┐
                                   │ Auto-Approved    │     │ Manual Required  │
                                   │ respond_yes()    │     │ register_button  │
                                   └──────────────────┘     └────────┬─────────┘
                                                                    │
                                                                    ▼
                                                           ┌──────────────────┐
                                                           │ Telegram Message │
                                                           │ [Approve][Deny]  │
                                                           └────────┬─────────┘
                                                                    │
                                                                    ▼
                                                           ┌──────────────────┐
                                                           │ User Click       │
                                                           │ handle_callback  │
                                                           └────────┬─────────┘
                                                                    │
                                                                    ▼
                                                           ┌──────────────────┐
                                                           │ HookSocketServer │
                                                           │ respond_permission│
                                                           └──────────────────┘
```

## 12. Session 状态同步流程

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         Session 状态同步流程                                     │
└─────────────────────────────────────────────────────────────────────────────────┘

┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│ Claude CLI       │     │ SessionSupervisor │     │ ClaudeJSONLParser│
│ JSONL Files      │────►│ sync_claude_     │────►│ parse_incremental│
└──────────────────┘     │ session()        │     └────────┬─────────┘
                         └──────────────────┘              │
                                                          ▼
                                                 ┌──────────────────┐
                                                 │ SessionEvent     │
                                                 │ FILE_SYNCED      │
                                                 └────────┬─────────┘
                                                          │
                                                          ▼
                                                 ┌──────────────────┐
                                                 │ SessionStore     │
                                                 │ process(event)   │
                                                 └────────┬─────────┘
                                                          │
                         ┌────────────────────────────────┼────────────────────────┐
                         │                                │                        │
                         ▼                                ▼                        ▼
                ┌──────────────────┐            ┌──────────────────┐    ┌──────────────────┐
                │SessionEvent      │            │SessionStateCache │    │SessionStateRepo  │
                │Processor         │            │invalidate()      │    │save()            │
                │apply_event()     │            └──────────────────┘    └────────┬─────────┘
                └──────────────────┘                                           │
                                                                              ▼
                                                                     ┌──────────────────┐
                                                                     │FileSessionStore  │
                                                                     │save(state)       │
                                                                     └──────────────────┘

                         ┌────────────────────────────────────────────────────────┐
                         │                                                        │
                         ▼                                                        ▼
                ┌──────────────────┐                                    ┌──────────────────┐
                │SessionNotifier   │                                    │StructuredReply   │
                │publish(state)    │                                    │Tracker           │
                └────────┬─────────┘                                    └──────────────────┘
                         │
                         ▼
                ┌──────────────────┐
                │Waiters Notified  │
                │wait_for_publish()│
                └──────────────────┘
```

这个完整的模块调用关系图展示了项目中各个模块之间的详细依赖和调用关系，包括数据流向和完整流程。
