from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path

from pydantic import ValidationError

from app.config.settings import Settings

# Single source of truth for the required configuration items, mapping each
# environment-variable alias to its corresponding `Settings` field name.
REQUIRED_FIELDS: dict[str, str] = {
    "TG_BOT_TOKEN": "tg_bot_token",
    "TG_ALLOWED_USER_IDS": "tg_allowed_user_ids",
}

# Default Env_File looked up from the current working directory when no
# `--env-file` path is provided (mirrors `Settings.model_config`'s env_file).
DEFAULT_ENV_FILE = ".env"


class StartupError(Exception):
    """A startup failure that carries a user-facing message.

    The message is intended to be printed to ``stderr`` by ``main()`` which
    then exits with a non-zero status. It must never surface as a raw
    traceback to the user.
    """


class EnvFileAction(StrEnum):
    """Classification of how an Env_File path should be handled.

    - ``ERROR_UNREADABLE``: an explicit path was given but cannot be read;
      loading must fail with a non-zero exit.
    - ``FALLBACK``: no explicit path was given and the default ``.env`` is
      absent; load from process environment variables only, without error.
    - ``LOAD``: load the (readable) Env_File alongside process environment
      variables.
    """

    ERROR_UNREADABLE = "ERROR_UNREADABLE"
    FALLBACK = "FALLBACK"
    LOAD = "LOAD"


def classify_env_file(
    env_file: str | None,
    is_readable: bool,
    *,
    default_env_exists: bool,
) -> EnvFileAction:
    """Classify how an Env_File path should be handled (pure function).

    The classification key is whether ``env_file`` is ``None``:

    - ``env_file`` is not ``None`` and ``is_readable`` is ``False`` ->
      ``ERROR_UNREADABLE``.
    - ``env_file`` is ``None`` and the default ``.env`` does not exist
      (``default_env_exists`` is ``False``) -> ``FALLBACK``.
    - every other combination -> ``LOAD``.

    The explicit-path error semantics and the default-missing fallback
    semantics never overlap: ``ERROR_UNREADABLE`` is reachable only when an
    explicit path is given, while ``FALLBACK`` is reachable only when no path
    is given.
    """
    if env_file is not None:
        return EnvFileAction.LOAD if is_readable else EnvFileAction.ERROR_UNREADABLE
    return EnvFileAction.LOAD if default_env_exists else EnvFileAction.FALLBACK


def missing_required_fields(env: Mapping[str, str], dotenv: Mapping[str, str]) -> list[str]:
    """Return required items missing/blank in *both* sources (pure function).

    Given the process environment mapping and the parsed Env_File mapping,
    return the subset of ``{TG_BOT_TOKEN, TG_ALLOWED_USER_IDS}`` whose value is
    missing or whitespace-only in both sources. The result preserves the stable
    declaration order of ``REQUIRED_FIELDS``.
    """
    missing: list[str] = []
    for alias in REQUIRED_FIELDS:
        if _is_blank(env.get(alias)) and _is_blank(dotenv.get(alias)):
            missing.append(alias)
    return missing


def load_settings(env_file: str | None) -> Settings:
    """Load :class:`Settings`, translating failures into :class:`StartupError`.

    - ``env_file is None``: construct ``Settings()`` using the default
      ``env_file=".env"`` from ``model_config``. When the default ``.env`` is
      absent, pydantic-settings silently falls back to process environment
      variables only (no error).
    - ``env_file is not None``: first verify the path exists and is readable,
      otherwise raise :class:`StartupError`. Then construct
      ``Settings(_env_file=env_file)`` so the explicit path overrides the
      ``model_config`` default without breaking the
      "process env var > dotenv" precedence.

    Any ``ValidationError`` raised while constructing ``Settings`` is caught and
    converted into a friendly :class:`StartupError`: missing/blank required
    fields are mapped back to their aliases with configuration guidance, and
    other validation errors are rendered as readable text rather than a raw
    traceback.
    """
    is_readable = _path_is_readable(env_file) if env_file is not None else False
    action = classify_env_file(env_file, is_readable, default_env_exists=Path(DEFAULT_ENV_FILE).is_file())

    if action is EnvFileAction.ERROR_UNREADABLE:
        raise StartupError(_unreadable_env_file_message(env_file))

    try:
        if action is EnvFileAction.LOAD and env_file is not None:
            return Settings(_env_file=env_file)  # type: ignore[call-arg]
        # FALLBACK, or LOAD of the default `.env`: rely on model_config default.
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        raise _startup_error_from_validation(exc) from exc


def _is_blank(value: str | None) -> bool:
    """Return ``True`` when *value* is ``None`` or whitespace-only."""
    return value is None or str(value).strip() == ""


def _path_is_readable(path: str) -> bool:
    """Return ``True`` when *path* points to an existing, readable file."""
    candidate = Path(path)
    return candidate.is_file() and os.access(candidate, os.R_OK)


def _startup_error_from_validation(exc: ValidationError) -> StartupError:
    """Build a friendly :class:`StartupError` from a pydantic validation error.

    Distinguishes truly *missing* required fields (``type == 'missing'``) from
    value errors on those same fields so the user sees "缺少必填配置项" only when
    the variable is absent, and "配置校验失败" when it is present but malformed.
    """
    missing = _required_aliases_in_error(exc, only_missing=True)
    if missing:
        return StartupError(_format_missing_message(missing))
    return StartupError(_format_other_validation_message(exc))


def _required_aliases_in_error(
    exc: ValidationError,
    *,
    only_missing: bool = False,
) -> list[str]:
    """Return required-field aliases implicated by *exc*, in declaration order.

    A validation error location may surface as either the alias
    (e.g. ``TG_BOT_TOKEN``) or the underlying field name (e.g. ``tg_bot_token``)
    depending on the error type, so both forms are mapped back to the alias.

    When *only_missing* is ``True``, only errors whose ``type`` is ``'missing'``
    are included — value errors on required fields are excluded so they surface
    as "配置校验失败" instead of "缺少必填配置项".
    """
    field_to_alias = {field: alias for alias, field in REQUIRED_FIELDS.items()}
    seen: set[str] = set()
    for err in exc.errors():
        if only_missing and err.get("type") != "missing":
            continue
        loc = err.get("loc") or ()
        if not loc:
            continue
        key = str(loc[0])
        if key in REQUIRED_FIELDS:
            seen.add(key)
        elif key in field_to_alias:
            seen.add(field_to_alias[key])
    return [alias for alias in REQUIRED_FIELDS if alias in seen]


def _format_missing_message(missing: list[str]) -> str:
    """Build the user-facing message for missing required configuration."""
    items = ", ".join(missing)
    return (
        f"缺少必填配置项：{items}\n\n"
        "请通过以下任一方式提供这些配置：\n"
        "  1) 进程环境变量，例如：export TG_BOT_TOKEN=<token> TG_ALLOWED_USER_IDS=<ids>\n"
        "  2) Env_File：在运行目录放置 .env，或使用 tg-cli-gateway --env-file /path/to/.env\n"
        "（同名配置项以进程环境变量为准；TG_ALLOWED_USER_IDS 可用 * 代表允许所有用户）"
    )


def _format_other_validation_message(exc: ValidationError) -> str:
    """Render non-required validation errors as friendly, readable text."""
    lines = ["配置校验失败，请检查以下配置项："]
    for err in exc.errors():
        loc = ".".join(str(part) for part in (err.get("loc") or ())) or "<unknown>"
        msg = err.get("msg", "无效取值")
        lines.append(f"  - {loc}: {msg}")
    return "\n".join(lines)


def _unreadable_env_file_message(env_file: str | None) -> str:
    """Build the user-facing message for an unreadable explicit Env_File."""
    return f"无法加载指定的 Env_File：{env_file}\n请确认该路径存在且当前用户对其拥有读取权限。"
