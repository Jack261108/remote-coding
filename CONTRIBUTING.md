# 贡献指南

感谢您对本项目的关注！本文档将指导您如何为项目做出贡献。

## 开发环境设置

### 1. 克隆仓库

```bash
git clone https://github.com/Jack261108/remote-coding.git
cd remote-coding
```

### 2. 创建虚拟环境

```bash
# 使用 pyenv 创建虚拟环境
pyenv virtualenv 3.11.0 remote-coding
pyenv local remote-coding
```

### 3. 安装依赖

```bash
# 安装项目和开发依赖
pip install -e ".[dev]"

# 安装 pre-commit hooks
pre-commit install
```

### 4. 验证环境

```bash
# 运行质量检查
./scripts/quality_check.sh
```

## 开发流程

### 1. 创建分支

```bash
# 从 master 分支创建功能分支
git checkout master
git pull origin master
git checkout -b feature/your-feature-name
```

### 2. 开发代码

- 遵循代码风格指南
- 编写测试用例
- 更新文档

### 3. 提交代码

```bash
# 添加文件
git add .

# 提交（pre-commit 会自动运行检查）
git commit -m "feat: 添加新功能"

# 推送
git push origin feature/your-feature-name
```

### 4. 创建 Pull Request

1. 在 GitHub 上创建 Pull Request
2. 填写 PR 模板
3. 等待代码审查
4. 根据反馈修改代码
5. 合并到 master 分支

## 代码风格

### Python 代码风格

- 遵循 PEP 8 规范
- 使用 Ruff 进行格式化
- 行长度限制: 140 字符

### 命名规范

- **变量和函数**: 小写字母 + 下划线 (snake_case)
- **类名**: 大驼峰 (CamelCase)
- **常量**: 大写字母 + 下划线 (UPPER_SNAKE_CASE)
- **私有成员**: 单下划线前缀 (_private)

### 类型注解

- 所有函数必须有类型注解
- 使用 `typing` 模块定义复杂类型
- 示例:

```python
from typing import Optional, List

def process_data(data: List[str], limit: Optional[int] = None) -> dict:
    """处理数据并返回结果。"""
    # 实现
    return {"result": "processed"}
```

### 文档字符串

- 使用 Google 风格的文档字符串
- 示例:

```python
def calculate_sum(numbers: List[int]) -> int:
    """计算数字列表的总和。

    Args:
        numbers: 数字列表

    Returns:
        数字总和

    Raises:
        ValueError: 如果列表为空
    """
    if not numbers:
        raise ValueError("列表不能为空")
    return sum(numbers)
```

## 测试要求

### 测试覆盖率

- 最低覆盖率要求: 80%
- 新功能必须包含测试
- 测试必须通过才能合并

### 测试类型

1. **单元测试**: 测试单个函数或方法
2. **集成测试**: 测试多个组件的交互
3. **端到端测试**: 测试完整功能流程

### 编写测试

```python
import pytest
from app.module import function_to_test

def test_function_with_valid_input():
    """测试函数在有效输入下的行为。"""
    result = function_to_test("valid_input")
    assert result == "expected_output"

def test_function_with_invalid_input():
    """测试函数在无效输入下的行为。"""
    with pytest.raises(ValueError):
        function_to_test("invalid_input")
```

### 运行测试

```bash
# 运行所有测试
pytest tests/

# 运行特定测试文件
pytest tests/test_module.py

# 运行带覆盖率的测试
pytest tests/ --cov=app --cov-report=html
```

## 提交规范

### 提交消息格式

```
<type>(<scope>): <subject>

<body>

<footer>
```

### 类型 (type)

- **feat**: 新功能
- **fix**: 修复 bug
- **docs**: 文档更新
- **style**: 代码风格修改（不影响功能）
- **refactor**: 代码重构
- **perf**: 性能优化
- **test**: 测试相关
- **chore**: 构建过程或辅助工具的变动

### 示例

```
feat(auth): 添加用户登录功能

- 实现用户名密码登录
- 添加 JWT token 生成
- 添加登录验证中间件

Closes #123
```

## 代码审查

### 审查清单

- [ ] 代码风格符合规范
- [ ] 类型注解完整
- [ ] 测试覆盖充分
- [ ] 文档已更新
- [ ] 无安全漏洞
- [ ] 性能可接受

### 审查流程

1. 提交 PR
2. 自动运行 CI 检查
3. 人工代码审查
4. 修改并重新提交
5. 合并到 master

## 问题报告

### 报告 Bug

1. 使用 GitHub Issues
2. 提供复现步骤
3. 提供错误日志
4. 提供环境信息

### 功能请求

1. 使用 GitHub Issues
2. 描述使用场景
3. 说明预期行为
4. 提供替代方案

## 发布流程

### 版本号规范

遵循语义化版本:

- **MAJOR**: 不兼容的 API 修改
- **MINOR**: 向后兼容的功能性新增
- **PATCH**: 向后兼容的问题修正

### 发布步骤

1. 更新版本号
2. 更新 CHANGELOG
3. 创建 Git tag
4. 推送到 GitHub
5. 创建 GitHub Release

## 行为准则

### 我们的承诺

- 尊重所有参与者
- 接受建设性批评
- 关注对社区最有利的事情
- 对他人表示同理心

### 不可接受的行为

- 使用性暗示的语言或图像
- 恶意攻击或侮辱
- 公开或私下骚扰
- 未经许可发布他人私人信息

## 许可证

本项目采用 MIT 许可证。贡献代码即表示您同意将代码置于 MIT 许可证下。

## 联系方式

如有任何问题，请通过以下方式联系:

- GitHub Issues: https://github.com/Jack261108/remote-coding/issues
- 邮箱: [待添加]

感谢您的贡献！