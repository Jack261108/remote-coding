# 发布 CI 设计

## 目标

为当前仓库新增一个独立的 GitHub Actions 发布流程：当推送 `v*` tag 时，自动创建对应的 GitHub Release，并使用 GitHub 自动生成的 release notes 和源码压缩包。

## 范围

包含：

- 新增独立 release workflow。
- tag 触发发布。
- 自动创建 GitHub Release。
- 自动生成 release notes。
- 使用 GitHub Release 默认提供的源码 zip/tar.gz。

不包含：

- 每次 push 到 `master` 自动发布。
- 构建并上传 Python wheel/sdist。
- 自动覆盖、删除或重发已有 Release。
- 发布到 PyPI 或其他包仓库。

## 架构

新增 `.github/workflows/release.yml`，不修改现有 `.github/workflows/ci.yml`。现有 CI 继续负责日常的 lint、type check 和 test；release workflow 只负责正式发布。

release workflow 使用仓库默认的 `GITHUB_TOKEN`，权限限定为 `contents: write`，用于创建 GitHub Release。workflow 不引入第三方 release action，直接使用 GitHub-hosted runner 上的 `gh` CLI。

## 触发方式

仅在推送匹配 `v*` 的 tag 时触发，例如：

```bash
git tag v0.1.0
git push origin v0.1.0
```

普通分支 push 和 pull request 不会触发发布。

## 发布流程

1. 用户完成代码变更并推送到 `master`。
2. 现有 CI 运行并验证日常检查。
3. 用户创建并推送 `v*` tag。
4. `release.yml` 被 tag push 触发。
5. workflow checkout 仓库。
6. workflow 执行 `gh release create "$GITHUB_REF_NAME" --generate-notes --verify-tag`。
7. GitHub 创建对应 Release，并自动提供源码 zip/tar.gz 下载项。

## 失败处理

- 非 `v*` tag 不触发 workflow。
- 同名 Release 已存在时，`gh release create` 失败，避免覆盖已发布版本。
- token 权限不足时，job 失败并显示 GitHub CLI 错误。
- workflow 不做自动删除、覆盖或重发，避免误改已发布版本。

## 验证方式

本地验证：

- 检查新增 workflow 的 YAML 内容。
- 确认工作区只包含预期文件变更。

远程验证：

- 推送普通 commit 后，不应触发 release workflow。
- 推送 `v*` tag 后，应触发 release workflow。
- workflow 成功后，GitHub Release 页面应出现对应版本、自动 release notes 和源码 zip/tar.gz。

## 后续扩展

如果未来需要发布 Python 包，可在 release workflow 中增加 `python -m build`，并把 `dist/*.whl` 和 `dist/*.tar.gz` 作为 Release assets 上传。当前设计不包含这一步，以保持发布流程最小化。
