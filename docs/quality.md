# 代码质量标准

本文档定义了项目的代码质量标准和检查流程。

## 质量检查工具

### 1. Ruff - 代码风格和格式

- **用途**: 静态代码分析、代码风格检查、自动格式化
- **配置**: `pyproject.toml` 中的 `[tool.ruff]` 部分
- **运行**: `python -m ruff check app/ tests/`
- **格式化**: `python -m ruff format app/ tests/`

### 2. Mypy - 类型检查

- **用途**: 静态类型检查，确保类型安全
- **配置**: `pyproject.toml` 中的 `[tool.mypy]` 部分
- **运行**: `python -m mypy app/`

### 3. Pytest - 测试框架

- **用途**: 单元测试、集成测试
- **配置**: `pyproject.toml` 中的 `[tool.pytest.ini_options]` 部分
- **运行**: `python -m pytest tests/`

### 4. Coverage - 测试覆盖率

- **用途**: 测量代码测试覆盖率
- **配置**: `pyproject.toml` 中的 `[tool.coverage.report]` 部分
- **最低要求**: 80%

## 质量检查流程

### 本地开发

1. **提交前检查** (pre-commit):
   - Ruff 代码风格检查
   - Ruff 格式化检查

2. **推送前检查** (pre-push):
   - Mypy 类型检查
   - Pytest 测试运行

### CI/CD 流程

1. **GitHub Actions** 自动运行:
   - Ruff lint 和 format 检查
   - Mypy 类型检查
   - Pytest 测试和覆盖率检查

2. **质量门禁**:
   - 所有检查必须通过
   - 测试覆盖率不低于 80%
   - 无类型错误
   - 无代码风格问题

## 质量标准

### 代码风格

- 遵循 PEP 8 规范
- 使用 Ruff 进行自动格式化
- 行长度限制: 140 字符

### 类型安全

- 所有函数必须有类型注解
- 使用 `typing` 模块定义复杂类型
- 禁止 `Any` 类型的滥用

### 测试要求

- 新功能必须包含测试
- 测试覆盖率不低于 80%
- 测试必须通过才能合并

### 代码审查

- 所有代码变更必须经过审查
- 审查者必须检查:
  - 代码风格
  - 类型安全
  - 测试覆盖
  - 文档更新

## 工具配置

### Ruff 配置

```toml
[tool.ruff]
target-version = "py311"
line-length = 140

[tool.ruff.lint]
select = ["E4", "E7", "E9", "F", "I", "UP", "B"]
```

### Mypy 配置

```toml
[tool.mypy]
python_version = "3.11"
show_error_codes = true
pretty = true
warn_unused_ignores = true
ignore_missing_imports = true
```

### Pytest 配置

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

### Coverage 配置

```toml
[tool.coverage.report]
fail_under = 80
```

## 运行质量检查

### 一键检查

```bash
# 运行完整质量检查
./scripts/quality_check.sh
```

### 分步检查

```bash
# 1. Ruff 检查
python -m ruff check app/ tests/
python -m ruff format --check app/ tests/

# 2. Mypy 检查
python -m mypy app/

# 3. 测试运行
python -m pytest tests/ -x -q --tb=short

# 4. 覆盖率检查
python -m pytest tests/ --cov=app --cov-report=term-missing --cov-fail-under=80
```

## 常见问题

### Q: Ruff 检查失败怎么办？

```bash
# 自动修复
python -m ruff check --fix app/ tests/
python -m ruff format app/ tests/
```

### Q: Mypy 检查失败怎么办？

1. 检查类型注解是否正确
2. 添加缺失的类型注解
3. 使用 `# type: ignore` 忽略特定错误（谨慎使用）

### Q: 测试覆盖率不足怎么办？

1. 运行覆盖率报告查看未覆盖代码
```bash
python -m pytest tests/ --cov=app --cov-report=html
```
2. 为未覆盖的代码添加测试
3. 重点关注核心业务逻辑

### Q: 如何跳过本地钩子检查？

紧急情况下可跳过本地钩子：

```bash
# 跳过 pre-push 检查（mypy + pytest，不推荐）
git push --no-verify

# 单次跳过 pre-commit 检查（ruff）
git commit --no-verify -m "commit message"
```

注意：`--no-verify` 只会跳过**本地**钩子，**远端 CI 仍会执行完整的检查集**（ruff lint、ruff format 校验、mypy、pytest）。因此该选项只是绕过本地的提前反馈，并不能跳过 CI。详见 [README「开发 / 本地钩子」](../README.md#开发--本地钩子pre-commit)。

## 最佳实践

1. **提交前检查**: 始终运行 `./scripts/quality_check.sh`
2. **小步提交**: 频繁提交，每次提交保持代码质量
3. **测试先行**: 先写测试，再写实现
4. **代码审查**: 所有代码变更必须经过审查
5. **持续改进**: 定期更新质量标准和工具配置