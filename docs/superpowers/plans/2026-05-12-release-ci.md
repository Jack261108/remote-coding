# Release CI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增基于 `v*` tag 的 GitHub Actions 发布流程，自动创建 GitHub Release，并使用自动生成的 release notes 和 GitHub 默认源码压缩包。

**Architecture:** 新增独立 `.github/workflows/release.yml`，不改现有 `.github/workflows/ci.yml`。日常 CI 继续负责 lint/type/test；Release workflow 只在 tag push 时运行，用仓库内置 `GITHUB_TOKEN` 和 runner 预装 `gh` CLI 创建 Release。

**Tech Stack:** GitHub Actions YAML, GitHub CLI `gh`, GitHub `GITHUB_TOKEN`, existing Python test/lint/type-check commands.

---

## File Structure

- Create: `.github/workflows/release.yml`
  - 责任：在推送 `v*` tag 时创建 GitHub Release。
  - 不构建 Python wheel/sdist，不上传自定义 asset。
  - Release 的源码 zip/tar.gz 由 GitHub 自动提供。
- No change: `.github/workflows/ci.yml`
  - 继续负责现有 lint、type check、test。
- No change: `pyproject.toml`
  - 当前版本号和包配置不参与本次发布 workflow 实现。

## Task 1: Add tag-based Release workflow

**Files:**
- Create: `.github/workflows/release.yml`
- Test: local content check and whitespace check

- [ ] **Step 1: Create the release workflow file**

Create `.github/workflows/release.yml` with this exact content:

```yaml
name: Release

on:
  push:
    tags:
      - "v*"

permissions:
  contents: write

jobs:
  release:
    name: Create GitHub Release
    runs-on: ubuntu-latest

    steps:
      - name: Check out repository
        uses: actions/checkout@v4

      - name: Create release
        env:
          GH_TOKEN: ${{ github.token }}
        run: gh release create "$GITHUB_REF_NAME" --generate-notes --verify-tag
```

- [ ] **Step 2: Verify the workflow contains the required release behavior**

Run:

```bash
python - <<'PY'
from pathlib import Path

path = Path('.github/workflows/release.yml')
text = path.read_text()
required = [
    'name: Release',
    'on:',
    'push:',
    'tags:',
    '- "v*"',
    'permissions:',
    'contents: write',
    'GH_TOKEN: ${{ github.token }}',
    'gh release create "$GITHUB_REF_NAME" --generate-notes --verify-tag',
]
missing = [item for item in required if item not in text]
if missing:
    raise SystemExit(f'missing release workflow content: {missing}')
print('release workflow content ok')
PY
```

Expected:

```text
release workflow content ok
```

- [ ] **Step 3: Verify the workflow diff has no whitespace errors**

Run:

```bash
git diff --check -- .github/workflows/release.yml
```

Expected: no output and exit code `0`.

## Task 2: Run local verification and commit the workflow

**Files:**
- Verify: `.github/workflows/release.yml`
- Verify: existing app and tests remain green

- [ ] **Step 1: Confirm the Python virtual environment before running Python checks**

Run:

```bash
python -c 'import os, sys; print(sys.executable); print(os.environ.get("VIRTUAL_ENV", ""))'
```

Expected: the executable path points to the project Python environment, and the second line is not empty.

If the second line is empty, run these commands before continuing:

```bash
pyenv virtualenv 3.11.13 remote-coding
pyenv local remote-coding
python -m pip install -e ".[dev]"
```

Expected after setup: rerunning the environment check prints a project virtual environment path.

- [ ] **Step 2: Run the existing lint command**

Run:

```bash
python -m ruff check app tests
```

Expected:

```text
All checks passed!
```

- [ ] **Step 3: Run the existing type check command**

Run:

```bash
python -m mypy --follow-imports=skip app/adapters/process/subprocess_runner.py app/bot/middleware/auth.py app/bot/middleware/rate_limit.py app/bot/handlers/command_permission.py app/bot/handlers/command_user_question.py app/bootstrap.py app/services/task_service.py
```

Expected:

```text
Success: no issues found in 7 source files
```

- [ ] **Step 4: Run the full test suite**

Run:

```bash
python -m pytest -q
```

Expected: all tests pass, matching the existing suite count for the current checkout.

- [ ] **Step 5: Review the final diff**

Run:

```bash
git diff -- .github/workflows/release.yml
```

Expected diff contains only the new Release workflow shown in Task 1.

- [ ] **Step 6: Commit the workflow**

Run:

```bash
git add .github/workflows/release.yml
git commit -m "$(cat <<'EOF'
ci: add tag-based release workflow
EOF
)"
```

Expected: one new commit that creates `.github/workflows/release.yml`.

## Task 3: Push implementation and verify GitHub sees the workflow

**Files:**
- Remote verification: `Jack261108/remote-coding`

This task affects the remote repository. Get explicit user confirmation before running the push.

- [ ] **Step 1: Push the implementation commits**

Run after confirmation:

```bash
git push origin master
```

Expected: `master` updates on `origin`.

- [ ] **Step 2: Verify the Release workflow is available on GitHub**

Run:

```bash
gh workflow view release.yml --repo Jack261108/remote-coding --yaml
```

Expected: output includes:

```text
name: Release
```

and includes the tag trigger:

```text
- "v*"
```

- [ ] **Step 3: Verify normal branch push did not create a Release run**

Run:

```bash
gh run list --repo Jack261108/remote-coding --workflow Release --limit 1
```

Expected: no new Release workflow run from the normal `master` push. If output is empty, this expectation is satisfied.

## Task 4: Live release verification by tag

**Files:**
- Remote verification: `Jack261108/remote-coding` Releases

This task creates a real GitHub Release. Run it only after the user explicitly confirms the release tag.

- [ ] **Step 1: Confirm the selected tag does not already exist**

For the current project version, use `v0.1.0` unless the user chooses a different tag.

Run:

```bash
tag_output="$(git ls-remote --tags origin v0.1.0)"
printf '%s\n' "$tag_output"
if [ -n "$tag_output" ]; then
  echo "tag v0.1.0 already exists"
  exit 1
fi

if gh release view v0.1.0 --repo Jack261108/remote-coding >/tmp/remote-coding-release-view.txt 2>&1; then
  cat /tmp/remote-coding-release-view.txt
  echo "release v0.1.0 already exists"
  exit 1
fi
cat /tmp/remote-coding-release-view.txt
echo "tag and release are available"
```

Expected:

```text
tag and release are available
```

If the command exits `1`, stop and ask the user for a new version tag.

- [ ] **Step 2: Create and push the release tag**

Run after confirmation:

```bash
git tag v0.1.0
git push origin v0.1.0
```

Expected: the tag is pushed to GitHub.

- [ ] **Step 3: Watch the Release workflow run**

Run:

```bash
run_id="$(gh run list --repo Jack261108/remote-coding --workflow Release --limit 1 --json databaseId --jq '.[0].databaseId')"
printf 'watching run %s\n' "$run_id"
gh run watch "$run_id" --repo Jack261108/remote-coding --exit-status
```

Expected: the selected Release workflow run completes successfully.

- [ ] **Step 4: Verify the GitHub Release exists**

Run:

```bash
gh release view v0.1.0 --repo Jack261108/remote-coding
```

Expected: output shows Release `v0.1.0` with generated notes. GitHub provides source code downloads for zip and tar.gz on the Release page.

## Self-Review Notes

- Spec coverage: Task 1 implements the independent release workflow, `v*` tag trigger, generated notes, and GitHub-provided source archives. Task 3 covers remote workflow visibility. Task 4 covers live tag release verification.
- Scope check: no wheel/sdist build, no PyPI publishing, no overwrite/delete release behavior, and no changes to existing CI.
- Risk handling: remote push and live tag release are separated into gated tasks because they affect shared GitHub state.
