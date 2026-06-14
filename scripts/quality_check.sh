#!/bin/bash
# 代码质量门禁脚本

set -e

echo "=== 代码质量检查 ==="

# 1. Ruff 代码风格检查
echo "1. 运行 Ruff 代码风格检查..."
ruff check app/ tests/

# 2. Ruff 格式检查
echo "2. 运行 Ruff 格式检查..."
ruff format --check app/ tests/

# 3. Mypy 类型检查
echo "3. 运行 Mypy 类型检查..."
mypy app/

# 4. 运行测试
echo "4. 运行测试套件..."
pytest tests/ -x -q --tb=short

# 5. 检查测试覆盖率
echo "5. 检查测试覆盖率..."
pytest tests/ --cov=app --cov-report=term-missing --cov-fail-under=80

echo "=== 所有检查通过 ==="