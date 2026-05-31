"""Deterministic consistency guard between CI and local pre-commit hooks.

This test parses ``.github/workflows/ci.yml`` and ``.pre-commit-config.yaml`` and
asserts that the two define a *semantically equivalent* set of checks (command
name, args, scope). Its purpose is to automatically flag any future one-sided
change to CI or the hooks that would break the "local pass => CI pass" guarantee
(see local-ci-parity-hooks spec, Requirements 1.6 and 4.2).

Design notes:
- Standard library ONLY. We deliberately do NOT use PyYAML (or any YAML library),
  even though pre-commit pulled it in transitively. The spec's non-goal is "no new
  check tooling / deps for this guard", so we use robust stdlib text/regex checks
  over the raw file contents instead of structurally parsing YAML.
- This is an example-level deterministic assertion, NOT a property-based test.
- We normalize whitespace before matching so the assertions stay resilient to
  incidental formatting (line continuations, YAML folded scalars ``>-``, and
  multi-line command lists).
- Ruff runs in read-only mode in CI (``ruff check`` / ``ruff format --check``) but
  in write mode in the hooks (``ruff check --fix`` / ``ruff format``). Per
  Requirements 1.3/1.4 those write-mode variants are explicitly allowed as
  semantic equivalents, so we assert tool+scope alignment (ruff check on
  ``app tests``, ruff format on ``app tests``) rather than byte-identical flags.
"""

from __future__ import annotations

from pathlib import Path
import re

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CI_PATH = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_PRECOMMIT_PATH = _REPO_ROOT / ".pre-commit-config.yaml"

# The canonical mypy file set, mirrored from the requirements glossary (Mypy_Check)
# and CI. Both CI and the hooks MUST cover exactly these 7 files.
_EXPECTED_MYPY_FILES = {
    "app/adapters/process/subprocess_runner.py",
    "app/bot/middleware/auth.py",
    "app/bot/middleware/rate_limit.py",
    "app/bot/handlers/command_permission.py",
    "app/bot/handlers/command_user_question.py",
    "app/bootstrap.py",
    "app/services/task_service.py",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _normalize(text: str) -> str:
    """Collapse all runs of whitespace (including newlines from line continuations
    and folded scalars) into a single space, and drop YAML folded-scalar markers.

    This makes substring matching resilient to how a command is wrapped across
    lines in either file.
    """
    # Drop YAML block/folded scalar indicators so "run: >-\n  python ..." reads as
    # "run: python ..." after whitespace collapsing.
    without_scalar_markers = re.sub(r">-|>\+|>|\|-|\|\+|\|", " ", text)
    return re.sub(r"\s+", " ", without_scalar_markers).strip()


def _canonicalize_ruff(normalized: str) -> str:
    """Remove ruff read/write-mode flags so the read-only (CI) and write-mode (hook)
    variants reduce to the same tool+scope form.

    ``ruff check --fix app tests``      -> ``ruff check app tests``
    ``ruff format --check app tests``   -> ``ruff format app tests``
    """
    stripped = re.sub(r"\s--fix\b", "", normalized)
    stripped = re.sub(r"\s--check\b", "", stripped)
    return re.sub(r"\s+", " ", stripped).strip()


def _mypy_files(text: str) -> set[str]:
    """Extract the set of ``app/.../*.py`` paths referenced by the file.

    In both ci.yml and .pre-commit-config.yaml the only ``app/*.py`` paths present
    are the mypy target files, so a global scan yields exactly the mypy file set.
    """
    return set(re.findall(r"app/[\w./-]+\.py", text))


def _hook_blocks(precommit_text: str) -> dict[str, dict[str, str]]:
    """Parse ``.pre-commit-config.yaml`` into a mapping of hook id -> {entry, stages}.

    Uses simple text splitting (no YAML library). Each hook starts at a ``- id:``
    marker; within a block we capture the (possibly folded) ``entry`` and the
    ``stages`` list as raw strings.
    """
    blocks: dict[str, dict[str, str]] = {}
    # Split on the "- id:" marker. The first chunk is the file preamble.
    chunks = re.split(r"(?m)^\s*-\s+id:\s*", precommit_text)[1:]
    for chunk in chunks:
        hook_id = chunk.splitlines()[0].strip()

        entry_match = re.search(r"entry:\s*(.*?)\n\s*language:", chunk, re.DOTALL)
        entry = _normalize(entry_match.group(1)) if entry_match else ""

        stages_match = re.search(r"stages:\s*\[([^\]]*)\]", chunk)
        stages = stages_match.group(1).replace(" ", "") if stages_match else ""

        blocks[hook_id] = {"entry": entry, "stages": stages}
    return blocks


def test_ci_defines_expected_check_commands() -> None:
    """CI (ci.yml) runs the four expected checks: ruff lint, ruff format check,
    mypy (read-only), and pytest."""
    ci = _normalize(_read(_CI_PATH))

    assert "python -m ruff check app tests" in ci
    assert "python -m ruff format --check app tests" in ci
    assert "python -m mypy --follow-imports=skip" in ci
    assert "python -m pytest -q" in ci


def test_precommit_defines_corresponding_hooks_in_correct_stages() -> None:
    """The hooks define the corresponding checks and place them in the stages that
    matter for parity: ruff in pre-commit; mypy + pytest in pre-push."""
    blocks = _hook_blocks(_read(_PRECOMMIT_PATH))

    # All four logical checks exist as local hooks.
    assert {"ruff-check", "ruff-format", "mypy", "pytest"} <= set(blocks)

    # pre-commit stage: fast ruff checks (Requirement 2.1).
    assert "ruff check --fix app tests" in blocks["ruff-check"]["entry"]
    assert blocks["ruff-check"]["stages"] == "pre-commit"
    assert "ruff format app tests" in blocks["ruff-format"]["entry"]
    assert blocks["ruff-format"]["stages"] == "pre-commit"

    # pre-push stage: slow checks (Requirement 3.1).
    assert "mypy --follow-imports=skip" in blocks["mypy"]["entry"]
    assert blocks["mypy"]["stages"] == "pre-push"
    assert "pytest -q" in blocks["pytest"]["entry"]
    assert blocks["pytest"]["stages"] == "pre-push"


def test_mypy_file_set_matches_between_ci_and_precommit() -> None:
    """The most valuable parity assertion: the mypy 7-file set must be identical on
    both sides. This catches a file being added/removed on only one side.
    """
    ci_files = _mypy_files(_read(_CI_PATH))
    hook_files = _mypy_files(_read(_PRECOMMIT_PATH))

    # Both sides must cover exactly the canonical 7 files...
    assert ci_files == _EXPECTED_MYPY_FILES
    assert hook_files == _EXPECTED_MYPY_FILES
    # ...and therefore must agree with each other.
    assert ci_files == hook_files


def test_ruff_tool_and_scope_alignment() -> None:
    """Ruff tool+scope must align between CI and hooks, independent of the
    read-only vs write-mode flag difference (Requirements 1.3/1.4 allow write-mode
    semantic equivalents). After stripping ``--fix``/``--check`` both sides reduce
    to the same ``ruff check app tests`` / ``ruff format app tests`` forms.
    """
    ci = _canonicalize_ruff(_normalize(_read(_CI_PATH)))
    hooks = _canonicalize_ruff(_normalize(_read(_PRECOMMIT_PATH)))

    assert "ruff check app tests" in ci
    assert "ruff format app tests" in ci
    assert "ruff check app tests" in hooks
    assert "ruff format app tests" in hooks
