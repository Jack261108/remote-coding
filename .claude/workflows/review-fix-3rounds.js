export const meta = {
  name: 'review-fix-3rounds',
  description: 'ultracode 3轮代码审查和修复：审查、修复、验证循环',
  phases: [
    { title: '第一轮审查', detail: '全面审查重构后的代码' },
    { title: '第一轮修复', detail: '修复发现的问题' },
    { title: '第二轮审查', detail: '深入审查修复后的代码' },
    { title: '第二轮修复', detail: '修复发现的问题' },
    { title: '第三轮审查', detail: '最终审查和验证' },
    { title: '第三轮修复', detail: '最终修复和优化' },
    { title: '最终验证', detail: '运行完整测试套件' },
  ],
}

// ==================== 第一轮：全面审查 ====================
phase('第一轮审查')

const round1Issues = await agent(
  `全面审查 worktree 中重构后的代码，找出所有问题。

  重点检查：
  1. 新创建的中间件文件：
     - app/bot/middleware/error_handling.py
     - app/bot/middleware/session_guard.py
     - app/bot/middleware/callback_validator.py

  2. 新创建的基础设施文件：
     - app/infra/periodic_task.py
     - app/infra/file_mtime_utils.py
     - app/infra/gitignore_utils.py

  3. 新创建的领域模型：
     - app/domain/session_tombstone.py

  4. 新创建的服务：
     - app/services/external_binding_cleanup_task.py
     - app/services/janitor_task.py

  5. 修改的处理器文件：
     - app/bot/handlers/*.py
     - app/bot/router.py

  6. 修改的服务文件：
     - app/services/auto_approve_service.py
     - app/services/external_session_discovery.py
     - app/services/external_binding_reaper.py
     - app/services/periodic_janitor.py

  检查维度：
  - 类型注解是否完整和正确
  - 导入是否必要且正确
  - 文档字符串是否完整
  - 错误处理是否适当
  - 代码风格是否一致
  - 是否有潜在的 bug
  - 是否有性能问题
  - 是否有安全隐患

  输出格式：JSON，包含 {issues: [{file, line, severity, category, description, suggestion}]}
  severity: critical, major, minor, info
  category: type, import, docstring, error, style, bug, performance, security
  `,
  { label: 'round1-review', phase: '第一轮审查', schema: {
    type: 'object',
    properties: {
      issues: { type: 'array', items: { type: 'object', properties: {
        file: { type: 'string' },
        line: { type: 'number' },
        severity: { type: 'string', enum: ['critical', 'major', 'minor', 'info'] },
        category: { type: 'string', enum: ['type', 'import', 'docstring', 'error', 'style', 'bug', 'performance', 'security'] },
        description: { type: 'string' },
        suggestion: { type: 'string' }
      }}}
    }
  }}
)

// ==================== 第一轮：修复 ====================
phase('第一轮修复')

await agent(
  `修复第一轮审查发现的问题。

  问题列表：
  ${JSON.stringify(round1Issues.issues, null, 2)}

  修复优先级：
  1. critical 问题必须修复
  2. major 问题应该修复
  3. minor 问题可以修复
  4. info 问题记录但不修复

  修复规则：
  - 不要破坏现有功能
  - 保持代码风格一致
  - 添加必要的类型注解
  - 添加必要的文档字符串
  - 修复潜在的 bug
  - 优化性能问题

  执行修复操作并报告修复结果。
  `,
  { label: 'round1-fix', phase: '第一轮修复' }
)

// ==================== 第二轮：深入审查 ====================
phase('第二轮审查')

const round2Issues = await agent(
  `深入审查第一轮修复后的代码，找出剩余问题。

  重点检查：
  1. 第一轮修复是否引入新问题
  2. 中间件的集成是否正确
  3. 后台任务的生命周期管理是否正确
  4. 墓碑存储的使用是否一致
  5. 类型注解的完整性和准确性
  6. 错误处理的一致性
  7. 日志记录的一致性
  8. 并发安全性

  特别关注：
  - app/bot/router.py 中的中间件注册顺序
  - app/bootstrap.py 中的服务初始化和依赖注入
  - 处理器中从 data 获取 session 的正确性
  - PeriodicBackgroundTask 的 start/stop 调用时机
  - SessionTombstoneStore 的 TTL 配置

  输出格式：JSON，包含 {issues: [{file, line, severity, category, description, suggestion}]}
  `,
  { label: 'round2-review', phase: '第二轮审查', schema: {
    type: 'object',
    properties: {
      issues: { type: 'array', items: { type: 'object', properties: {
        file: { type: 'string' },
        line: { type: 'number' },
        severity: { type: 'string', enum: ['critical', 'major', 'minor', 'info'] },
        category: { type: 'string', enum: ['type', 'import', 'docstring', 'error', 'style', 'bug', 'performance', 'security'] },
        description: { type: 'string' },
        suggestion: { type: 'string' }
      }}}
    }
  }}
)

// ==================== 第二轮：修复 ====================
phase('第二轮修复')

await agent(
  `修复第二轮审查发现的问题。

  问题列表：
  ${JSON.stringify(round2Issues.issues, null, 2)}

  修复规则同第一轮。

  执行修复操作并报告修复结果。
  `,
  { label: 'round2-fix', phase: '第二轮修复' }
)

// ==================== 第三轮：最终审查 ====================
phase('第三轮审查')

const round3Issues = await agent(
  `最终审查，确保代码质量达标。

  检查维度：
  1. 所有 critical 和 major 问题是否已修复
  2. 代码是否符合项目规范
  3. 测试是否覆盖新代码
  4. 文档是否完整
  5. 是否有遗漏的重构机会

  额外检查：
  - 运行类型检查：mypy app/
  - 运行代码质量检查：ruff check app/
  - 检查测试覆盖率

  输出格式：JSON，包含 {issues: [{file, line, severity, category, description, suggestion}], type_check: {passed, errors}, lint_check: {passed, errors}}
  `,
  { label: 'round3-review', phase: '第三轮审查', schema: {
    type: 'object',
    properties: {
      issues: { type: 'array', items: { type: 'object', properties: {
        file: { type: 'string' },
        line: { type: 'number' },
        severity: { type: 'string', enum: ['critical', 'major', 'minor', 'info'] },
        category: { type: 'string', enum: ['type', 'import', 'docstring', 'error', 'style', 'bug', 'performance', 'security'] },
        description: { type: 'string' },
        suggestion: { type: 'string' }
      }}},
      type_check: { type: 'object', properties: { passed: { type: 'boolean' }, errors: { type: 'array', items: { type: 'string' } } } },
      lint_check: { type: 'object', properties: { passed: { type: 'boolean' }, errors: { type: 'array', items: { type: 'string' } } } }
    }
  }}
)

// ==================== 第三轮：最终修复 ====================
phase('第三轮修复')

await agent(
  `修复第三轮审查发现的问题。

  问题列表：
  ${JSON.stringify(round3Issues.issues, null, 2)}

  类型检查错误：
  ${JSON.stringify(round3Issues.type_check.errors, null, 2)}

  代码质量检查错误：
  ${JSON.stringify(round3Issues.lint_check.errors, null, 2)}

  修复规则：
  - 修复所有 remaining critical 和 major 问题
  - 修复类型检查错误
  - 修复代码质量检查错误
  - minor 和 info 问题可选择性修复

  执行修复操作并报告修复结果。
  `,
  { label: 'round3-fix', phase: '第三轮修复' }
)

// ==================== 最终验证 ====================
phase('最终验证')

const testResult = await agent(
  `运行完整测试套件验证所有修复。

  步骤：
  1. 运行所有测试：python -m pytest tests/ -v --tb=short
  2. 检查测试结果
  3. 如果有测试失败，分析原因并修复
  4. 运行类型检查：mypy app/ --ignore-missing-imports
  5. 运行代码质量检查：ruff check app/

  输出格式：JSON，包含 {tests: {passed, failed, errors}, type_check: {passed, errors}, lint_check: {passed, errors}, summary: string}
  `,
  { label: 'final-verification', phase: '最终验证', schema: {
    type: 'object',
    properties: {
      tests: { type: 'object', properties: { passed: { type: 'number' }, failed: { type: 'number' }, errors: { type: 'array', items: { type: 'string' } } } },
      type_check: { type: 'object', properties: { passed: { type: 'boolean' }, errors: { type: 'array', items: { type: 'string' } } } },
      lint_check: { type: 'object', properties: { passed: { type: 'boolean' }, errors: { type: 'array', items: { type: 'string' } } } },
      summary: { type: 'string' }
    }
  }}
)

// 生成最终报告
const report = await agent(
  `生成 3 轮代码审查和修复的最终报告。

  第一轮发现的问题：${round1Issues.issues.length} 个
  第二轮发现的问题：${round2Issues.issues.length} 个
  第三轮发现的问题：${round3Issues.issues.length} 个

  最终验证结果：
  ${JSON.stringify(testResult, null, 2)}

  请生成一份结构化的报告，包含：
  1. 执行摘要
  2. 每轮审查的发现和修复
  3. 最终代码质量指标
  4. 改进建议

  使用 Markdown 格式输出。
  `,
  { label: 'generate-report', phase: '最终验证' }
)

return { report, round1Issues, round2Issues, round3Issues, testResult }
