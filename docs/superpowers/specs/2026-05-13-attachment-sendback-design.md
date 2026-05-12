# 附件回传设计

## 目标

为当前 Telegram CLI Gateway 增加 cc-connect 风格的附件回传能力：当 Agent 生成本地截图、图表、PDF、报告或压缩包后，用户可以通过 Telegram 命令把这些文件发送回当前聊天。

首版采用显式命令触发，不做任务结束后的自动扫描，避免误发敏感文件。

## 范围

包含：

- 新增 Telegram 命令 `/sendfile <path> [path...]`。
- 新增 Telegram 命令 `/sendimage <path> [path...]`。
- 新增全局开关 `ATTACHMENT_SEND`，默认开启。
- 新增单文件大小限制 `ATTACHMENT_MAX_BYTES`，默认 `20971520`（20MB）。
- 限制附件路径必须位于 `ALLOWED_WORKDIRS` 内。
- 限制只发送普通文件。
- `/sendimage` 仅允许常见图片后缀。
- `/sendfile` 仅允许安全白名单后缀。
- 更新 README 和 `.env.example`，说明用法与限制。
- 添加单元测试覆盖配置、路径校验和 Telegram 发送调用。

不包含：

- 自动扫描任务输出目录。
- 自动发送新生成文件。
- 多平台附件发送抽象。
- 本地 CLI 子命令，例如 `tg-cli-gateway send --file ...`。
- MCP 工具或 Claude Code tool 注册。
- 文件压缩、打包或格式转换。

## cc-connect 参考点

cc-connect 的附件回传采用显式命令模式：

```bash
cc-connect send --image /absolute/path/to/chart.png
cc-connect send --file /absolute/path/to/report.pdf
cc-connect send --file /absolute/path/to/report.pdf --image /absolute/path/to/chart.png
```

并通过独立配置开关控制：

```toml
attachment_send = "on"
```

本项目首版借鉴其关键原则：

- 附件发送是独立能力，不影响普通文本回复。
- Agent 或用户必须显式指定要发送的文件。
- 推荐绝对路径，避免依赖当前工作目录。
- 文件必须存在于 Agent 所在机器。
- 平台大小和类型限制仍然适用。

由于当前项目是 Telegram 专用服务，首版不增加本地 CLI，而是直接提供 Telegram 命令。

## 命令设计

### `/sendfile`

用法：

```text
/sendfile /absolute/path/to/report.pdf
/sendfile /absolute/path/to/report.pdf /absolute/path/to/bundle.zip
```

行为：

- 解析一个或多个路径。
- 对每个路径执行安全校验。
- 通过 Telegram document 发送。
- 每个文件独立发送；某个文件失败不阻止后续文件继续尝试。
- 命令结束后返回成功/失败摘要。

### `/sendimage`

用法：

```text
/sendimage /absolute/path/to/chart.png
/sendimage /absolute/path/to/a.png /absolute/path/to/b.jpg
```

行为：

- 解析一个或多个路径。
- 对每个路径执行安全校验。
- 通过 Telegram photo 发送。
- 只接受图片后缀。
- 命令结束后返回成功/失败摘要。

## 配置设计

在 `Settings` 中新增：

```python
attachment_send: bool = Field(True, alias="ATTACHMENT_SEND")
attachment_max_bytes: int = Field(20 * 1024 * 1024, alias="ATTACHMENT_MAX_BYTES")
```

校验规则：

- `ATTACHMENT_SEND` 使用现有布尔解析规则，支持 `true/false`、`1/0`、`yes/no`、`on/off`。
- `ATTACHMENT_MAX_BYTES` 必须大于 0。

`.env.example` 新增：

```env
# Attachment send-back
ATTACHMENT_SEND=true
ATTACHMENT_MAX_BYTES=20971520
```

当 `ATTACHMENT_SEND=false` 时：

- `/sendfile` 和 `/sendimage` 均返回“附件回传已关闭”。
- 普通文本回复、任务执行、权限审批不受影响。

## 安全边界

### 路径限制

每个附件路径必须满足：

- 路径解析后位于任一 `ALLOWED_WORKDIRS` 内。
- 文件存在。
- 是普通文件。
- 不是目录、设备文件、socket、FIFO 等特殊文件。
- 解析后的真实路径用于安全判断，避免 `..` 和符号链接逃逸。

路径判断复用现有 `is_workdir_allowed()` 逻辑，保持与任务工作目录限制一致。

### 类型限制

`/sendimage` 允许：

- `.png`
- `.jpg`
- `.jpeg`
- `.webp`
- `.gif`

`/sendfile` 允许：

- `.pdf`
- `.txt`
- `.md`
- `.csv`
- `.json`
- `.log`
- `.zip`
- `.tar`
- `.gz`
- `.tgz`

不发送代码、配置、环境变量、密钥、数据库文件等高风险后缀。无后缀文件默认拒绝。

### 大小限制

单文件大小不得超过 `ATTACHMENT_MAX_BYTES`。

默认 20MB，低于 Telegram Bot 常见限制，减少发送失败概率。

## 架构设计

新增一个小型服务和一个 Telegram handler，避免把校验逻辑塞进路由函数。

### `AttachmentService`

建议文件：`app/services/attachment_service.py`

职责：

- 校验附件开关。
- 解析路径。
- 检查路径是否在 `ALLOWED_WORKDIRS` 内。
- 检查文件存在性、普通文件、大小、后缀。
- 返回结构化校验结果。

不直接依赖 aiogram，不发送 Telegram 消息。

### `command_attachment` handler

建议文件：`app/bot/handlers/command_attachment.py`

职责：

- 注册 `/sendfile` 和 `/sendimage`。
- 解析命令参数。
- 调用 `AttachmentService` 校验。
- 使用 aiogram 发送文件：
  - `/sendimage` 使用 `FSInputFile` + `message.answer_photo()`。
  - `/sendfile` 使用 `FSInputFile` + `message.answer_document()`。
- 汇总每个文件的发送结果。

### 路由装配

`app/bot/router.py` 中注册新 handler：

```python
register_attachment_handlers(router, attachment_service=attachment_service)
```

`create_router()` 需要接收 `attachment_service` 参数。

`AppContainer` 负责创建 `AttachmentService` 并传入 router。

## 数据流

1. 用户发送 `/sendfile /path/to/report.pdf`。
2. AuthMiddleware 先校验 Telegram 用户白名单。
3. handler 解析路径参数。
4. handler 调用 `AttachmentService.validate_file(path, kind="file")`。
5. service 返回可发送文件或拒绝原因。
6. handler 对通过校验的文件调用 Telegram API。
7. handler 返回摘要，例如：

```text
附件发送完成
成功: 1
失败: 1
- /path/to/secret.env: 文件类型不允许
```

## 错误处理

用户可见错误保持简短明确：

- 参数为空：`用法: /sendfile <path> [path...]`
- 功能关闭：`附件回传已关闭。`
- 文件不存在：`文件不存在`
- 路径越界：`文件不在 ALLOWED_WORKDIRS 白名单内`
- 非普通文件：`不是普通文件`
- 文件过大：`文件超过大小限制`
- 后缀不允许：`文件类型不允许`
- Telegram API 发送失败：`发送失败: <错误摘要>`

发送多个文件时，单个失败不影响其他文件。

## 测试设计

新增或扩展测试：

- `Settings` 能解析 `ATTACHMENT_SEND` 和 `ATTACHMENT_MAX_BYTES`。
- `ATTACHMENT_MAX_BYTES <= 0` 被拒绝。
- `.env.example` 包含附件配置。
- `AttachmentService` 接受 `ALLOWED_WORKDIRS` 内的合法文件。
- `AttachmentService` 拒绝越界路径。
- `AttachmentService` 拒绝不存在路径。
- `AttachmentService` 拒绝目录。
- `AttachmentService` 拒绝超大文件。
- `AttachmentService` 拒绝 `/sendimage` 的非图片后缀。
- handler 在 `/sendfile` 中调用 document 发送方法。
- handler 在 `/sendimage` 中调用 photo 发送方法。
- 配置关闭时 handler 不发送附件并返回关闭提示。

## 验证方式

本地验证：

```bash
python -m ruff check app tests
python -m mypy --follow-imports=skip app/adapters/process/subprocess_runner.py app/bot/middleware/auth.py app/bot/middleware/rate_limit.py app/bot/handlers/command_permission.py app/bot/handlers/command_user_question.py app/bootstrap.py app/services/task_service.py
pytest -q
```

手动验证：

1. 在 `ALLOWED_WORKDIRS` 内创建一张小图片，发送 `/sendimage <path>`，Telegram 收到图片。
2. 在 `ALLOWED_WORKDIRS` 内创建 PDF 或 txt，发送 `/sendfile <path>`，Telegram 收到文件。
3. 发送白名单外路径，收到拒绝提示。
4. 设置 `ATTACHMENT_SEND=false`，确认命令被拒绝且普通文本任务不受影响。

## 后续扩展

首版完成后，可再考虑：

- 增加本地 CLI：`python -m app.send --file ...`，供 Agent 在 shell 中直接调用。
- 给 Claude 注入更强的系统提示或项目记忆，让它知道何时使用附件命令。
- 增加“任务结束后提示候选文件”，但不自动发送。
- 增加多平台附件抽象。
- 支持发送说明文字和附件组合。
