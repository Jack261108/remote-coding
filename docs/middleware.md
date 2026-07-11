# 中间件使用指南

本文档介绍 Telegram Bot 中间件的使用方法和最佳实践。

## 中间件概述

中间件是 aiogram 框架提供的请求处理机制，可以在 handler 执行前后进行拦截和处理。
本项目使用中间件实现认证、限流、错误处理、会话管理等横切关注点。

## 中间件执行顺序

中间件按照注册顺序执行，形成处理管道：

```
请求进入
  ↓
AuthMiddleware          # 身份验证
  ↓
RateLimitMiddleware     # 请求限流
  ↓
ErrorHandlingMiddleware # 错误处理（最外层）
  ↓
SessionGuardMiddleware  # 会话守卫
  ↓
CallbackValidatorMiddleware # 回调验证（仅回调查询）
  ↓
Handler                 # 实际处理器
  ↓
响应返回
```

## 内置中间件

### 1. AuthMiddleware

**文件**: `app/bot/middleware/auth.py`

验证用户身份，只允许授权用户访问。

```python
from app.bot.middleware.auth import AuthMiddleware

auth = AuthMiddleware(
    allowed_user_ids={123456, 789012},
    allow_all_users=False,
)
router.message.middleware(auth)
router.callback_query.middleware(auth)
```

**参数**:
- `allowed_user_ids`: 允许的用户 ID 集合
- `allow_all_users`: 是否允许所有用户（开发模式）

### 2. RateLimitMiddleware

**文件**: `app/bot/middleware/rate_limit.py`

限制用户请求频率，防止滥用。

```python
from app.bot.middleware.rate_limit import RateLimitMiddleware

rate_limit = RateLimitMiddleware(
    limit=10,           # 时间窗口内最大请求数
    window_sec=60,      # 时间窗口（秒）
    bucket_ttl_sec=300, # 令牌桶过期时间
)
router.message.middleware(rate_limit)
router.callback_query.middleware(rate_limit)
```

**参数**:
- `limit`: 时间窗口内最大请求数
- `window_sec`: 时间窗口（秒）
- `bucket_ttl_sec`: 令牌桶过期时间（秒）
- `cleanup_interval_sec`: 清理间隔（秒）
- `cleanup_batch_size`: 每次清理的桶数量

### 3. ErrorHandlingMiddleware

**文件**: `app/bot/middleware/error_handling.py`

统一捕获 handler 异常，向用户返回友好错误消息。

```python
from app.bot.middleware.error_handling import ErrorHandlingMiddleware

error_handling = ErrorHandlingMiddleware()
router.message.middleware(error_handling)
router.callback_query.middleware(error_handling)
```

**异常处理策略**:
- `ValueError`: 记录 warning，回复具体错误描述
- `Exception`: 记录 exception（含 traceback），回复通用错误消息
- `BaseException`: 不捕获（如 KeyboardInterrupt、CancelledError）

### 4. SessionGuardMiddleware

**文件**: `app/bot/middleware/session_guard.py`

检查用户会话状态，可选要求会话处于活跃状态。

```python
from app.bot.middleware.session_guard import SessionGuardMiddleware

# 基础守卫：仅要求会话存在
guard_basic = SessionGuardMiddleware(
    session_service,
    require_active=False,
)

# 活跃守卫：要求会话处于活跃状态
guard_active = SessionGuardMiddleware(
    session_service,
    require_active=True,
)

router.message.middleware(guard_basic)
router.callback_query.middleware(guard_basic)

# 子路由使用活跃守卫
active_router = Router()
active_router.message.middleware(guard_active)
active_router.callback_query.middleware(guard_active)
router.include_router(active_router)
```

**参数**:
- `session_service`: 会话服务实例
- `require_active`: 是否要求会话处于活跃状态

**注入数据**:
- `data["session"]`: 验证通过后注入的 `SessionContext` 对象

### 5. CallbackValidatorMiddleware

**文件**: `app/bot/middleware/callback_validator.py`

验证回调数据格式，防止非法回调到达 handler。

```python
from app.bot.middleware.callback_validator import CallbackValidatorMiddleware

# 验证回调数据格式：3 段，首段以 "sess" 开头
session_callbacks = CallbackValidatorMiddleware(
    expected_parts=3,
    prefix="sess",
)

# 支持多个可接受的段数和前缀
user_question_callbacks = CallbackValidatorMiddleware(
    expected_parts=(4, 5),
    prefix="ask",
)

router.callback_query.middleware(session_callbacks)
```

**参数**:
- `expected_parts`: 期望的段数（单个整数或整数元组）
- `prefix`: 期望的前缀（单个字符串或字符串元组，可选）

**注入数据**:
- `data["callback_parts"]`: 验证通过后注入的拆分结果元组

## 自定义中间件

### 创建中间件类

```python
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from collections.abc import Awaitable, Callable
from typing import Any


class MyMiddleware(BaseMiddleware):
    """自定义中间件示例。"""

    def __init__(self, param: str) -> None:
        super().__init__()
        self._param = param

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict], Awaitable],
        event: TelegramObject,
        data: dict,
    ) -> Any:
        # 前置处理
        print(f"Before handler: {self._param}")

        # 注入数据到 handler
        data["my_param"] = self._param

        # 调用 handler
        result = await handler(event, data)

        # 后置处理
        print(f"After handler: {result}")

        return result
```

### 注册中间件

```python
from aiogram import Router

router = Router()

# 全局中间件
router.message.middleware(MyMiddleware("global"))
router.callback_query.middleware(MyMiddleware("global"))

# 子路由中间件
sub_router = Router()
sub_router.message.middleware(MyMiddleware("sub"))
router.include_router(sub_router)
```

## 最佳实践

### 1. 中间件顺序

- 认证中间件放在最前面
- 限流中间件紧随其后
- 错误处理中间件放在业务中间件外层
- 业务中间件（会话守卫等）放在最后

### 2. 错误处理

- 在最外层使用 `ErrorHandlingMiddleware` 捕获所有异常
- 业务中间件应尽量避免抛出异常
- 使用 `logger.exception()` 记录完整 traceback

### 3. 数据注入

- 使用 `data` 字典向 handler 注入数据
- 注入的键名应具有描述性（如 `session`、`callback_parts`）
- 在文档中明确说明注入的数据类型

### 4. 性能考虑

- 避免在中间件中进行耗时操作
- 使用缓存减少重复查询
- 合理设置限流参数

### 5. 测试

- 为每个中间件编写单元测试
- 测试正常流程和异常流程
- 测试中间件组合的行为

## 常见问题

### Q: 中间件不生效？

1. 检查中间件注册顺序
2. 确认中间件类型（message/callback_query）
3. 检查中间件是否正确调用 `handler(event, data)`

### Q: 如何跳过某些请求？

```python
async def __call__(self, handler, event, data):
    if should_skip(event):
        return await handler(event, data)
    # 正常处理
    ...
```

### Q: 如何访问注入的数据？

```python
async def my_handler(message: Message, session: SessionContext) -> None:
    # 通过函数参数名访问注入的数据
    print(session.session_id)
```
