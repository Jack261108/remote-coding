# Telegram CLI Gateway (Python + aiogram)

Telegram 交互式远程 CLI 执行网关：在 Telegram 下发任务，服务在远程 Linux 执行本机 CLI（Claude Code / Codex CLI / Gemini CLI），并将过程与结果回传。

## 功能

- Long Polling 接入 Telegram
- 统一 provider 适配层（`claude_code` / `codex` / `gemini`）
- 可选 Claude tmux 终端模式（可在本机 attach 查看真实终端）
- 安全执行：`subprocess_exec` 参数数组调用，禁止 `shell=True`
- 任务状态机：`PENDING -> RUNNING -> SUCCEEDED/FAILED/TIMEOUT/CANCELED`
- 命令：`/start`、`/run`、`/status`、`/cancel`、`/session`
- 白名单鉴权 + 用户维度限流
- 输出分片（<4096）+ 节流 + 结束总结
- 内存存储（预留后续扩展 sqlite）
- systemd 部署样例 + healthcheck 脚本

## 目录

- `app/main.py`：入口
- `app/bootstrap.py`：依赖装配
- `app/config/settings.py`：配置与校验
- `app/adapters/cli/*`：provider 适配器与工厂
- `app/adapters/process/subprocess_runner.py`：进程执行与事件流
- `app/services/task_service.py`：任务服务
- `app/services/session_service.py`：会话服务
- `app/bot/router.py`：路由
- `app/bot/handlers/*`：命令处理
- `app/bot/middleware/*`：鉴权与限流
- `app/bot/presenters/chunk_sender.py`：分片发送
- `deploy/env/.env.example`：配置模板
- `deploy/systemd/tg-cli-bot.service`：systemd unit
- `deploy/scripts/healthcheck.sh`：健康检查脚本

## 快速开始

1. 安装依赖

```bash
python3 -m pip install -e ".[dev]"
```

2. 配置环境变量

```bash
cp deploy/env/.env.example .env
# 修改 .env
```

3. 启动

```bash
python3 -m app.main
```

## Telegram 命令

- `/start`：帮助与当前 session
- `/run <provider> <task text>`：执行任务
- `/status [task_id]`：查询单任务/最近任务
- `/cancel <task_id>`：取消任务
- `/session [provider] [workdir]`：查看/切换 session

provider 支持：`claude_code`、`codex`、`gemini`（含常见别名）

若开启 `CLAUDE_TMUX_MODE=true`，`claude_code` 会在 tmux session 中运行，并在任务开始消息返回 `tmux_session`，可本机查看：

```bash
tmux attach -t <tmux_session>
```

## 安全边界

- 默认仅白名单用户（`TG_ALLOWED_USER_IDS`）可执行；设置为 `*` 可放开所有用户（仅建议本地调试）
- 网络受限时可配置 `TG_PROXY_URL`，并可通过 `TG_REQUEST_TIMEOUT_SEC` / `TG_POLLING_RETRY_DELAY_SEC` 调整连接超时与重试（配置代理需安装 `aiohttp-socks`）
- `CLAUDE_TMUX_MODE=true` 时需本机安装 tmux
- 工作目录必须在 `ALLOWED_WORKDIRS` 内
- 不使用 shell 拼接，避免命令注入
- 超时控制 + 取消控制
- 输出总量限制（`TASK_OUTPUT_CHAR_LIMIT`）

## 测试

```bash
pytest -q
```

当前用例覆盖：provider 映射、任务状态机、输出分片与节流。

## Linux systemd 部署

1. 将项目部署到 `/opt/tg-cli-gateway`
2. 准备 `.env` 与虚拟环境
3. 复制 `deploy/systemd/tg-cli-bot.service` 到 `/etc/systemd/system/`
4. 执行：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tg-cli-bot.service
sudo systemctl status tg-cli-bot.service
```

健康检查：

```bash
bash deploy/scripts/healthcheck.sh
```
