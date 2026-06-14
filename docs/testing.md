# 测试指南

本文档介绍项目的测试策略、测试工具和最佳实践。

## 测试结构

```
tests/
├── unit/                    # 单元测试
│   └── test_session_actions.py
├── property/                # 属性测试
│   └── test_auto_approve_slot_aba.py
├── test_bootstrap_hooks.py  # 集成测试
├── test_command_list.py
├── test_external_binding_reaper.py
├── test_file_upload_handler.py
├── test_pending_lock_cleanup.py
├── test_session_handlers.py
└── ...
```

## 测试类型

### 1. 单元测试

测试单个函数或类的行为，不依赖外部系统。

```python
# tests/unit/test_session_actions.py
import pytest
from app.domain.session_tombstone import SessionTombstoneStore


class TestSessionTombstoneStore:
    """SessionTombstoneStore 单元测试。"""

    def test_mark_ended(self) -> None:
        """测试标记会话已结束。"""
        store = SessionTombstoneStore(ttl_seconds=60)
        store.mark_ended("session-1")
        assert store.is_ended("session-1")

    def test_mark_unavailable(self) -> None:
        """测试标记会话不可用。"""
        store = SessionTombstoneStore(ttl_seconds=60)
        store.mark_unavailable("session-1")
        assert store.is_unavailable("session-1")

    def test_ttl_expiration(self) -> None:
        """测试 TTL 过期。"""
        store = SessionTombstoneStore(ttl_seconds=0)
        store.mark_ended("session-1")
        # TTL 为 0，立即过期
        assert not store.is_ended("session-1")

    def test_clear(self) -> None:
        """测试清除墓碑记录。"""
        store = SessionTombstoneStore(ttl_seconds=60)
        store.mark_ended("session-1")
        store.clear("session-1")
        assert not store.is_ended("session-1")
```

### 2. 属性测试

使用 Hypothesis 库进行基于属性的测试，自动生成测试数据。

```python
# tests/property/test_auto_approve_slot_aba.py
from hypothesis import given, strategies as st
from app.services.auto_approve_service import AutoApproveService


@given(
    session_id=st.text(min_size=1, max_size=50),
    slot_id=st.integers(min_value=0, max_value=100),
)
def test_auto_approve_slot_aba(session_id: str, slot_id: int) -> None:
    """测试自动审批服务的 ABA 问题防护。"""
    service = AutoApproveService()
    service.enable(session_id, slot_id)
    assert service.is_enabled(session_id, slot_id)
    service.disable(session_id, slot_id)
    assert not service.is_enabled(session_id, slot_id)
```

### 3. 集成测试

测试多个组件协作的行为，可能依赖外部系统。

```python
# tests/test_bootstrap_hooks.py
import pytest
from app.bootstrap import AppContainer
from app.config.settings import Settings


@pytest.fixture
def app_container(test_settings: Settings) -> AppContainer:
    """创建测试用的 AppContainer。"""
    container = AppContainer(test_settings)
    return container


async def test_hook_installation(app_container: AppContainer) -> None:
    """测试 Hook 安装。"""
    app_container.hook_installer.install()
    assert app_container.hook_installer.is_installed()
```

## 测试工具

### pytest

项目使用 pytest 作为测试框架。

```bash
# 运行所有测试
pytest

# 运行特定目录的测试
pytest tests/unit/

# 运行特定文件
pytest tests/test_session_handlers.py

# 运行特定测试类
pytest tests/unit/test_session_actions.py::TestSessionTombstoneStore

# 运行特定测试方法
pytest tests/unit/test_session_actions.py::TestSessionTombstoneStore::test_mark_ended

# 显示详细输出
pytest -v

# 显示 print 输出
pytest -s

# 失败后停止
pytest -x

# 只运行上次失败的测试
pytest --lf
```

### pytest-asyncio

用于测试异步代码。

```python
import pytest


@pytest.mark.asyncio
async def test_async_function() -> None:
    """测试异步函数。"""
    result = await some_async_function()
    assert result == expected_value
```

### pytest-mock

用于模拟依赖。

```python
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.fixture
def mock_session_service() -> MagicMock:
    """模拟会话服务。"""
    service = MagicMock()
    service.get = AsyncMock(return_value=None)
    return service


async def test_with_mock(mock_session_service: MagicMock) -> None:
    """使用模拟对象进行测试。"""
    result = await some_function(mock_session_service)
    mock_session_service.get.assert_called_once()
```

### Hypothesis

用于属性测试。

```python
from hypothesis import given, strategies as st


@given(data=st.data())
def test_property(data: st.DataObject) -> None:
    """基于属性的测试。"""
    value = data.draw(st.integers(min_value=0, max_value=100))
    assert 0 <= value <= 100
```

## 测试最佳实践

### 1. 测试命名

- 测试文件以 `test_` 开头
- 测试类以 `Test` 开头
- 测试方法以 `test_` 开头
- 使用描述性的名称说明测试意图

```python
# 好的命名
def test_mark_ended_sets_ended_flag() -> None: ...
def test_ttl_expiration_removes_record() -> None: ...

# 不好的命名
def test_1() -> None: ...
def test_function() -> None: ...
```

### 2. 测试结构

使用 AAA 模式（Arrange-Act-Assert）：

```python
def test_example() -> None:
    """测试示例。"""
    # Arrange - 准备测试数据
    store = SessionTombstoneStore(ttl_seconds=60)
    session_id = "session-1"

    # Act - 执行被测试的操作
    store.mark_ended(session_id)

    # Assert - 验证结果
    assert store.is_ended(session_id)
```

### 3. 测试隔离

- 每个测试应该独立运行
- 使用 fixture 设置和清理测试环境
- 避免测试之间的依赖

```python
@pytest.fixture
def tombstone_store() -> SessionTombstoneStore:
    """创建测试用的墓碑存储。"""
    store = SessionTombstoneStore(ttl_seconds=60)
    yield store
    # 清理（如果需要）
    store.clear("session-1")


def test_with_fixture(tombstone_store: SessionTombstoneStore) -> None:
    """使用 fixture 的测试。"""
    tombstone_store.mark_ended("session-1")
    assert tombstone_store.is_ended("session-1")
```

### 4. 测试覆盖

- 测试正常流程
- 测试边界条件
- 测试异常情况
- 测试并发场景（如果适用）

```python
class TestSessionTombstoneStore:
    """SessionTombstoneStore 测试。"""

    def test_normal_flow(self) -> None:
        """测试正常流程。"""
        store = SessionTombstoneStore(ttl_seconds=60)
        store.mark_ended("session-1")
        assert store.is_ended("session-1")

    def test_boundary_conditions(self) -> None:
        """测试边界条件。"""
        store = SessionTombstoneStore(ttl_seconds=0)
        store.mark_ended("session-1")
        assert not store.is_ended("session-1")

    def test_exception_handling(self) -> None:
        """测试异常处理。"""
        store = SessionTombstoneStore(ttl_seconds=60)
        # 测试不存在的会话
        assert not store.is_ended("nonexistent")
```

### 5. Mock 使用

- 只 mock 必要的依赖
- 验证 mock 的调用
- 使用 `AsyncMock` 处理异步函数

```python
from unittest.mock import AsyncMock, patch


async def test_with_patch() -> None:
    """使用 patch 进行测试。"""
    with patch("app.services.session_service.SessionService.get") as mock_get:
        mock_get.return_value = None
        result = await some_function()
        mock_get.assert_called_once()
```

## 测试配置

### pytest.ini

```ini
[pytest]
testpaths = tests
python_files = test_*.py
python_classes = Test*
python_functions = test_*
asyncio_mode = auto
```

### conftest.py

```python
# tests/conftest.py
import pytest
from app.config.settings import Settings


@pytest.fixture
def test_settings() -> Settings:
    """创建测试用的配置。"""
    return Settings(
        tg_bot_token="test-token",
        allowed_user_ids={123456},
        # 其他测试配置...
    )
```

## 持续集成

### GitHub Actions

```yaml
# .github/workflows/test.yml
name: Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.12'
      - run: pip install -e ".[dev]"
      - run: pytest --cov=app --cov-report=xml
      - uses: codecov/codecov-action@v3
```

## 调试测试

### 使用 pdb

```python
def test_debug() -> None:
    """调试测试。"""
    import pdb; pdb.set_trace()
    # 测试代码...
```

### 使用 pytest-pdb

```bash
# 失败时进入 pdb
pytest --pdb

# 第一个失败时停止
pytest -x --pdb
```

### 查看测试覆盖率

```bash
# 生成覆盖率报告
pytest --cov=app --cov-report=html

# 查看未覆盖的代码
pytest --cov=app --cov-report=term-missing
```

## 常见问题

### Q: 异步测试不工作？

确保：
1. 使用 `@pytest.mark.asyncio` 装饰器
2. 安装了 `pytest-asyncio`
3. 在 `pytest.ini` 中配置 `asyncio_mode = auto`

### Q: Mock 不生效？

1. 检查 patch 路径是否正确
2. 确认 mock 对象类型（MagicMock vs AsyncMock）
3. 验证 mock 是否被正确调用

### Q: 测试之间相互干扰？

1. 使用 fixture 设置和清理环境
2. 避免全局状态
3. 使用独立的测试数据
