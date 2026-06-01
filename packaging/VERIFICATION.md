# Homebrew 公式验证清单

本文件记录公式发布前后的手动/CI 验证步骤。

## 占位说明

`packaging/tg-cli-gateway.rb` 中的 `sha256` 为 `000...0`（64 个零），这是**有意的占位值**。
首次发布 `vX.Y.Z` 时，`release.yml` 会用 `git archive` 生成 tarball、计算真实 SHA-256、
并将占位值替换后推送到 tap 仓库 `Jack261108/homebrew-tg-cli-gateway`。

在首次发布完成之前，本仓库的公式**不能**直接用于 `brew install`——它是给 release pipeline
改写的模板。只有 tap 仓库中的公式才是用户实际安装的来源。

## 安装验证

```bash
# 从源码构建安装（需先完成首次发布，tap 仓库中有真实 sha256）
brew install --build-from-source tg-cli-gateway
# 预期：退出码 0，tg-cli-gateway 命令在 PATH 中可用
```

## 功能测试

```bash
# brew test（10 秒内完成）
brew test tg-cli-gateway
# 预期：退出码 0，输出包含版本号
```

## 审计

```bash
brew audit --strict --online tg-cli-gateway
# 预期：退出码 0，无 error 级别条目
```

## SHA-256 校验

故意修改公式中的 sha256 为错误值，然后执行 `brew install --build-from-source`：
- 预期：安装中止，退出码非 0，输出指示校验和不匹配

## 安装失败回滚

模拟安装中途失败（如断网），验证：
- `bin` 目录不留 `tg-cli-gateway` 可执行链接
- 已安装目录无残留

## 升级保留配置

```bash
# 创建用户 .env 文件（Homebrew 前缀之外）
echo "TG_BOT_TOKEN=test" > ~/.tg-cli-gateway.env

# 升级
brew upgrade tg-cli-gateway

# 验证 .env 文件内容不变
cat ~/.tg-cli-gateway.env
```

## 版本一致性

以下脚本须在应用仓库根目录下运行（非 tap 仓库），因为依赖 `scripts.release_check` 模块。

```bash
# 验证公式版本与 pyproject.toml 一致
python -c "
from scripts.release_check import read_pyproject_version, parse_formula_version
formula = open('packaging/tg-cli-gateway.rb').read()
pv = read_pyproject_version()
fv = parse_formula_version(formula)
assert pv == fv, f'mismatch: pyproject={pv}, formula={fv}'
print(f'version consistent: {pv}')
"
```

## 模板与 tap 公式同步

应用仓库的 `packaging/tg-cli-gateway.rb` 是模板，tap 仓库的
`Formula/tg-cli-gateway.rb` 是用户实际安装的公式。两者在非占位字段
（即除 `url` 和 `sha256` 以外的所有内容）必须保持一致。

```bash
# 比较模板与 tap 公式的非占位字段
diff \
  <(grep -v -E '^\s*(url|sha256) ' packaging/tg-cli-gateway.rb) \
  <(curl -sL https://raw.githubusercontent.com/Jack261108/homebrew-tg-cli-gateway/main/Formula/tg-cli-gateway.rb \
      | grep -v -E '^\s*(url|sha256) ')
# 预期：无输出（完全一致）
```

如果存在差异，说明有人改了其中一份而没有同步到另一份。
修改应以应用仓库的模板为准，然后通过重新发布（或手动更新 tap）来同步。
