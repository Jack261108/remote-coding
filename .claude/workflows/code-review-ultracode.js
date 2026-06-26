export const meta = {
  name: 'code-review-ultracode',
  description: '全面代码审查：冗余代码、可复用逻辑、代码质量、架构分析',
  phases: [
    { title: '冗余代码检测' },
    { title: '可复用逻辑识别' },
    { title: '代码质量审查' },
    { title: '架构分析' },
    { title: '综合报告' },
  ],
}

// 项目源代码目录（排除测试和缓存）
const SRC_DIRS = [
  'app/adapters',
  'app/bot',
  'app/config',
  'app/domain',
  'app/infra',
  'app/services',
  'app/bootstrap*.py',
  'app/main.py',
]

// 阶段 1: 冗余代码检测
phase('冗余代码检测')

// 1.1 检测重复的工具函数
const duplicateUtils = await agent(
  `分析 /Users/jack/project/remote-coding/app/ 目录下的所有 Python 文件。
  找出：
  1. 功能完全相同的函数（名称可能不同）
  2. 相似度超过 80% 的代码块
  3. 已存在但未使用的导入
  4. 已定义但未使用的函数/类

  特别关注：
  - infra/ 目录下的工具函数
  - services/ 目录下的通用逻辑
  - bot/handlers/ 下的重复模式

  输出格式：JSON，包含 {duplicates: [{file1, func1, file2, func2, similarity}], unused_imports: [{file, imports}], unused_funcs: [{file, funcs}]}
  `,
  { label: 'duplicate-detector', phase: '冗余代码检测', schema: {
    type: 'object',
    properties: {
      duplicates: { type: 'array', items: { type: 'object', properties: { file1: {type: 'string'}, func1: {type: 'string'}, file2: {type: 'string'}, func2: {type: 'string'}, similarity: {type: 'number'} } } },
      unused_imports: { type: 'array', items: { type: 'object', properties: { file: {type: 'string'}, imports: {type: 'array', items: {type: 'string'}} } } },
      unused_funcs: { type: 'array', items: { type: 'object', properties: { file: {type: 'string'}, funcs: {type: 'array', items: {type: 'string'}} } } }
    }
  }}
)

// 1.2 检测重复的错误处理模式
const duplicateErrorHandling = await agent(
  `分析 /Users/jack/project/remote-coding/app/services/ 和 app/bot/handlers/ 目录。
  找出重复的错误处理模式：
  1. 相似的 try-except 块
  2. 重复的日志记录代码
  3. 相似的验证逻辑

  输出格式：JSON，包含 {patterns: [{pattern_type, occurrences: [{file, lines}], suggestion}]}
  `,
  { label: 'error-pattern-detector', phase: '冗余代码检测', schema: {
    type: 'object',
    properties: {
      patterns: { type: 'array', items: { type: 'object', properties: { pattern_type: {type: 'string'}, occurrences: {type: 'array', items: {type: 'object'}}, suggestion: {type: 'string'} } } }
    }
  }}
)

// 阶段 2: 可复用逻辑识别
phase('可复用逻辑识别')

// 2.1 识别可提取为通用服务的逻辑
const reusableServices = await agent(
  `分析 /Users/jack/project/remote-coding/app/services/ 目录下的所有服务。
  找出：
  1. 多个服务中重复出现的业务逻辑
  2. 可以抽象为通用接口的模式
  3. 可以使用策略模式优化的条件分支
  4. 可以使用模板方法模式的相似流程

  特别关注：
  - session 相关服务
  - permission 相关服务
  - external binding 相关服务

  输出格式：JSON，包含 {extractable: [{pattern, services, suggestion}], abstractions: [{interface_name, methods, implementations}]}
  `,
  { label: 'reusable-service-detector', phase: '可复用逻辑识别', schema: {
    type: 'object',
    properties: {
      extractable: { type: 'array', items: { type: 'object', properties: { pattern: {type: 'string'}, services: {type: 'array', items: {type: 'string'}}, suggestion: {type: 'string'} } } },
      abstractions: { type: 'array', items: { type: 'object', properties: { interface_name: {type: 'string'}, methods: {type: 'array', items: {type: 'string'}}, implementations: {type: 'array', items: {type: 'string'}} } } }
    }
  }}
)

// 2.2 识别可提取为中间件/装饰器的通用逻辑
const middlewareOpportunities = await agent(
  `分析 /Users/jack/project/remote-coding/app/bot/handlers/ 目录下的所有处理器。
  找出：
  1. 重复的前置/后置处理逻辑
  2. 可以提取为装饰器的通用逻辑
  3. 可以提取为中间件的通用逻辑

  常见模式：
  - 权限检查
  - 管理员验证
  - 会话状态验证
  - 输入验证
  - 日志记录

  输出格式：JSON，包含 {decorators: [{name, logic, handlers}], middleware: [{name, logic, applies_to}]}
  `,
  { label: 'middleware-detector', phase: '可复用逻辑识别', schema: {
    type: 'object',
    properties: {
      decorators: { type: 'array', items: { type: 'object', properties: { name: {type: 'string'}, logic: {type: 'string'}, handlers: {type: 'array', items: {type: 'string'}} } } },
      middleware: { type: 'array', items: { type: 'object', properties: { name: {type: 'string'}, logic: {type: 'string'}, applies_to: {type: 'string'} } } }
    }
  }}
)

// 阶段 3: 代码质量审查
phase('代码质量审查')

// 3.1 检查代码复杂度
const complexityIssues = await agent(
  `分析 /Users/jack/project/remote-coding/app/ 目录下的所有 Python 文件。
  检查：
  1. 过长的函数（超过 50 行）
  2. 过深的嵌套（超过 3 层）
  3. 过多的参数（超过 5 个）
  4. 过长的文件（超过 300 行）
  5. 过多的方法（类方法超过 10 个）

  输出格式：JSON，包含 {long_funcs: [{file, func, lines}], deep_nesting: [{file, func, depth}], many_params: [{file, func, count}], long_files: [{file, lines}], large_classes: [{file, cls, methods}]}
  `,
  { label: 'complexity-analyzer', phase: '代码质量审查', schema: {
    type: 'object',
    properties: {
      long_funcs: { type: 'array', items: { type: 'object', properties: { file: {type: 'string'}, func: {type: 'string'}, lines: {type: 'number'} } } },
      deep_nesting: { type: 'array', items: { type: 'object', properties: { file: {type: 'string'}, func: {type: 'string'}, depth: {type: 'number'} } } },
      many_params: { type: 'array', items: { type: 'object', properties: { file: {type: 'string'}, func: {type: 'string'}, count: {type: 'number'} } } },
      long_files: { type: 'array', items: { type: 'object', properties: { file: {type: 'string'}, lines: {type: 'number'} } } },
      large_classes: { type: 'array', items: { type: 'object', properties: { file: {type: 'string'}, cls: {type: 'string'}, methods: {type: 'number'} } } }
    }
  }}
)

// 3.2 检查命名规范
const namingIssues = await agent(
  `分析 /Users/jack/project/remote-coding/app/ 目录下的所有 Python 文件。
  检查命名规范：
  1. 类名应使用 PascalCase
  2. 函数名应使用 snake_case
  3. 常量应使用 UPPER_SNAKE_CASE
  4. 私有方法应以 _ 开头
  5. 避免使用单字符变量名（除了循环变量）

  输出格式：JSON，包含 {class_naming: [{file, name, issue}], func_naming: [{file, name, issue}], var_naming: [{file, name, issue}]}
  `,
  { label: 'naming-checker', phase: '代码质量审查', schema: {
    type: 'object',
    properties: {
      class_naming: { type: 'array', items: { type: 'object', properties: { file: {type: 'string'}, name: {type: 'string'}, issue: {type: 'string'} } } },
      func_naming: { type: 'array', items: { type: 'object', properties: { file: {type: 'string'}, name: {type: 'string'}, issue: {type: 'string'} } } },
      var_naming: { type: 'array', items: { type: 'object', properties: { file: {type: 'string'}, name: {type: 'string'}, issue: {type: 'string'} } } }
    }
  }}
)

// 阶段 4: 架构分析
phase('架构分析')

// 4.1 检查依赖关系
const dependencyIssues = await agent(
  `分析 /Users/jack/project/remote-coding/app/ 目录下的模块依赖关系。
  检查：
  1. 循环依赖
  2. 违反分层架构（如 domain 依赖 infra）
  3. 跨层直接调用（如 handler 直接调用 adapter）
  4. 依赖倒置原则违反

  当前架构分层：
  - domain: 领域模型和协议
  - services: 业务服务
  - adapters: 外部适配器
  - bot: Telegram bot 相关
  - infra: 基础设施
  - config: 配置

  输出格式：JSON，包含 {circular_deps: [{cycle}], layer_violations: [{from, to, type}], dip_violations: [{high_level, low_level, suggestion}]}
  `,
  { label: 'dependency-analyzer', phase: '架构分析', schema: {
    type: 'object',
    properties: {
      circular_deps: { type: 'array', items: { type: 'object', properties: { cycle: {type: 'array', items: {type: 'string'}} } } },
      layer_violations: { type: 'array', items: { type: 'object', properties: { from: {type: 'string'}, to: {type: 'string'}, type: {type: 'string'} } } },
      dip_violations: { type: 'array', items: { type: 'object', properties: { high_level: {type: 'string'}, low_level: {type: 'string'}, suggestion: {type: 'string'} } } }
    }
  }}
)

// 4.2 检查单一职责原则
const srpViolations = await agent(
  `分析 /Users/jack/project/remote-coding/app/services/ 目录下的所有服务类。
  检查单一职责原则违反：
  1. 一个类承担了多个不相关的职责
  2. 一个方法做了多件不相关的事情
  3. 类名暗示了多个职责（如 XxxAndYyy）

  输出格式：JSON，包含 {violations: [{file, class, responsibilities, suggestion}]}
  `,
  { label: 'srp-checker', phase: '架构分析', schema: {
    type: 'object',
    properties: {
      violations: { type: 'array', items: { type: 'object', properties: { file: {type: 'string'}, class: {type: 'string'}, responsibilities: {type: 'array', items: {type: 'string'}}, suggestion: {type: 'string'} } } }
    }
  }}
)

// 阶段 5: 综合报告
phase('综合报告')

const report = await agent(
  `基于以下审查结果，生成一份全面的代码审查报告。

  ## 冗余代码检测结果
  ${JSON.stringify(duplicateUtils, null, 2)}

  ## 重复错误处理模式
  ${JSON.stringify(duplicateErrorHandling, null, 2)}

  ## 可复用服务逻辑
  ${JSON.stringify(reusableServices, null, 2)}

  ## 中间件/装饰器机会
  ${JSON.stringify(middlewareOpportunities, null, 2)}

  ## 代码复杂度问题
  ${JSON.stringify(complexityIssues, null, 2)}

  ## 命名规范问题
  ${JSON.stringify(namingIssues, null, 2)}

  ## 依赖关系问题
  ${JSON.stringify(dependencyIssues, null, 2)}

  ## 单一职责违反
  ${JSON.stringify(srpViolations, null, 2)}

  请生成一份结构化的报告，包含：
  1. 执行摘要（关键发现和优先级）
  2. 冗余代码清单（按严重程度排序）
  3. 可复用逻辑机会（按收益排序）
  4. 代码质量问题（按影响排序）
  5. 架构改进建议（按紧急程度排序）
  6. 具体的重构建议（包含代码示例）

  使用 Markdown 格式输出。
  `,
  { label: 'report-generator', phase: '综合报告' }
)

return { report, raw_data: { duplicateUtils, duplicateErrorHandling, reusableServices, middlewareOpportunities, complexityIssues, namingIssues, dependencyIssues, srpViolations } }
