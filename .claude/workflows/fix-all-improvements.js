export const meta = {
  name: 'fix-all-improvements',
  description: 'ultracode 修复所有改进项：类型错误、测试、文档、模块化、质量门禁',
  phases: [
    { title: '修复 Mypy 类型错误' },
    { title: '新增模块单元测试' },
    { title: '提升测试覆盖率' },
    { title: '完善文档' },
    { title: '模块化复杂函数' },
    { title: '建立质量门禁' },
    { title: '最终验证' },
  ],
}

// ==================== 短期 1: 修复 Mypy 类型错误 ====================
phase('修复 Mypy 类型错误')

await agent(
  `修复 Mypy 类型检查发现的所有错误。

  错误列表：
  1. app/bot/middleware/callback_validator.py:40 - override 错误
     - __call__ 的 event 参数类型收窄为 CallbackQuery，违反 BaseMiddleware 签名
     - 修复：保持参数类型为 TelegramObject，内部用 isinstance 收窄

  2. app/services/external_binding_reaper.py:102 - union-attr 错误
     - self._external_discovery 类型为 ExternalSessionDiscoveryService | None
     - lambda 中直接访问 .remove_session() 时 mypy 无法确认 None 收窄
     - 修复：在 if 守卫内先绑定局部变量 discovery = self._external_discovery

  3. app/services/external_binding_reaper.py:106 - union-attr 错误
     - self._permission_callback_registry 类型为 PermissionCallbackRegistry | None
     - 修复：同上，绑定局部变量

  4. app/services/external_binding_reaper.py:109 - union-attr 错误
     - self._external_uq_state 类型为 ExternalUserQuestionState | None
     - 修复：同上，绑定局部变量

  5. app/services/external_binding_reaper.py:113 - union-attr 错误
     - self._external_discovery 类型为 ExternalSessionDiscoveryService | None
     - 修复：同上，绑定局部变量

  执行修复操作，确保所有 Mypy 错误都被解决。
  `,
  { label: 'fix-mypy-errors', phase: '修复 Mypy 类型错误' }
)

// ==================== 短期 2: 新增模块单元测试 ====================
phase('新增模块单元测试')

await agent(
  `为新增模块创建单元测试。

  需要测试的模块：
  1. app/domain/session_tombstone.py
  2. app/infra/periodic_task.py
  3. app/bot/middleware/callback_validator.py
  4. app/bot/middleware/session_guard.py
  5. app/infra/file_mtime_utils.py
  6. app/infra/gitignore_utils.py

  为每个模块创建测试文件，覆盖：
  - 核心功能路径
  - 边界条件
  - 错误处理
  - 并发安全（如适用）

  测试文件命名：
  - tests/unit/test_session_tombstone.py
  - tests/unit/test_periodic_task.py
  - tests/unit/test_callback_validator.py
  - tests/unit/test_session_guard.py
  - tests/unit/test_file_mtime_utils.py
  - tests/unit/test_gitignore_utils.py

  使用 pytest 和 pytest-asyncio 编写测试。
  执行创建操作。
  `,
  { label: 'create-unit-tests', phase: '新增模块单元测试' }
)

// ==================== 中期 1: 提升测试覆盖率 ====================
phase('提升测试覆盖率')

await agent(
  `分析当前测试覆盖率，找出覆盖不足的代码路径。

  步骤：
  1. 运行 pytest --cov=app --cov-report=term-missing
  2. 分析覆盖率报告，找出未覆盖的代码
  3. 为关键业务逻辑添加测试用例

  重点关注：
  - 新创建的中间件
  - 新创建的基础设施类
  - 修改的处理器逻辑
  - 边界条件和错误处理

  为每个未覆盖的关键路径添加测试。
  执行分析和补充操作。
  `,
  { label: 'improve-coverage', phase: '提升测试覆盖率' }
)

// ==================== 中期 2: 完善文档 ====================
phase('完善文档')

await agent(
  `完善代码文档。

  需要完善的文档：

  1. 模块级文档字符串：
     - app/bot/middleware/error_handling.py
     - app/bot/middleware/session_guard.py
     - app/bot/middleware/callback_validator.py
     - app/infra/periodic_task.py
     - app/infra/file_mtime_utils.py
     - app/infra/gitignore_utils.py
     - app/domain/session_tombstone.py
     - app/services/external_binding_cleanup_task.py
     - app/services/janitor_task.py

  2. 类和方法文档字符串：
     - 所有公共类的 __init__ 方法
     - 所有公共方法
     - 所有抽象方法

  3. 添加类型注解：
     - 确保所有公共函数有完整的类型注解
     - 使用 TypeVar 和泛型提升类型安全

  4. 创建架构文档：
     - docs/architecture.md - 整体架构说明
     - docs/middleware.md - 中间件使用指南
     - docs/testing.md - 测试指南

  执行完善操作。
  `,
  { label: 'improve-docs', phase: '完善文档' }
)

// ==================== 中期 3: 模块化复杂函数 ====================
phase('模块化复杂函数')

await agent(
  `分析并模块化复杂函数。

  目标函数（按行数排序）：
  1. bot/handlers/command_run.py:run_prompt_and_stream (314 行)
  2. bot/router.py:create_router (278 行)
  3. bot/handlers/command_list.py:register_list_handler (227 行)
  4. bootstrap.py:__init__ (215 行)
  5. adapters/process/tmux_runner.py:_watch_task (190 行)

  模块化策略：
  1. 提取子函数：将长函数拆分为多个小函数
  2. 提取类：将相关逻辑封装为类
  3. 使用策略模式：替换复杂的条件分支
  4. 使用模板方法模式：统一相似流程

  对每个目标函数：
  1. 分析函数结构
  2. 识别可提取的逻辑块
  3. 创建子函数或类
  4. 重构原函数调用新创建的组件
  5. 确保测试通过

  执行模块化操作。
  `,
  { label: 'modularize-functions', phase: '模块化复杂函数' }
)

// ==================== 长期 3: 建立质量门禁 ====================
phase('建立质量门禁')

await agent(
  `建立代码质量门禁。

  步骤 1: 创建质量检查脚本 scripts/quality_check.sh
  \`\`\`bash
  #!/bin/bash
  # 代码质量门禁脚本

  set -e

  echo "=== 代码质量检查 ==="

  # 1. Ruff 代码风格检查
  echo "1. 运行 Ruff 代码风格检查..."
  ruff check app/ tests/

  # 2. Mypy 类型检查
  echo "2. 运行 Mypy 类型检查..."
  mypy app/ --ignore-missing-imports

  # 3. 运行测试
  echo "3. 运行测试套件..."
  pytest tests/ -x -q --tb=short

  # 4. 检查测试覆盖率
  echo "4. 检查测试覆盖率..."
  pytest tests/ --cov=app --cov-report=term-missing --cov-fail-under=80

  echo "=== 所有检查通过 ==="
  \`\`\`

  步骤 2: 创建 pre-commit hook 配置
  - 创建 .pre-commit-config.yaml
  - 配置 ruff、mypy、pytest hooks

  步骤 3: 创建 CI/CD 配置
  - 创建 .github/workflows/quality.yml
  - 配置 GitHub Actions 运行质量检查

  步骤 4: 创建开发文档
  - docs/quality.md - 质量标准和检查流程
  - CONTRIBUTING.md - 贡献指南

  步骤 5: 配置 IDE 集成
  - 创建 .vscode/settings.json 配置
  - 创建 pyproject.toml 中的工具配置

  执行创建操作。
  `,
  { label: 'create-quality-gates', phase: '建立质量门禁' }
)

// ==================== 最终验证 ====================
phase('最终验证')

await agent(
  `运行最终验证。

  步骤：
  1. 运行完整测试套件：pytest tests/ -v
  2. 运行 Mypy 类型检查：mypy app/ --ignore-missing-imports
  3. 运行 Ruff 代码风格检查：ruff check app/ tests/
  4. 检查测试覆盖率：pytest tests/ --cov=app --cov-report=term-missing
  5. 验证所有新创建的测试通过

  如果有任何检查失败，分析原因并修复。

  输出验证结果报告。
  `,
  { label: 'final-verification', phase: '最终验证' }
)

await agent(
  `生成改进完成报告。

  包含：
  1. 修复的 Mypy 类型错误列表
  2. 新增的单元测试列表
  3. 测试覆盖率提升情况
  4. 完善的文档列表
  5. 模块化的函数列表
  6. 建立的质量门禁说明
  7. 最终质量指标

  使用 Markdown 格式输出。
  `,
  { label: 'generate-report', phase: '最终验证' }
)

return { status: 'All improvements completed' }
