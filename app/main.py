from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import tomllib
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from aiogram.exceptions import TelegramNetworkError

from app.bootstrap import AppContainer
from app.config.loader import StartupError, load_settings
from app.config.settings import Settings
from app.infra.logging import configure_logging
from app.infra.tmux_preflight import check_tmux_preflight

logger = logging.getLogger(__name__)

PROG = "tg-cli-gateway"


@dataclass(frozen=True)
class CliOptions:
    """Parsed command-line options for the CLI executable.

    `env_file` holds the path given via `--env-file`; it is `None` when the
    option is not provided.
    """

    env_file: str | None


def get_version() -> str:
    """Return the application version.

    The single source of truth is `pyproject.toml`'s `[project].version`.

    When running from a source checkout (a ``pyproject.toml`` is found nearby),
    read it directly so that the version always matches the current checkout,
    even if a different version of the package happens to be installed in the
    environment. When no ``pyproject.toml`` is found (installed package context),
    fall back to the installed package metadata.

    Catches all exceptions from pyproject reading (file not found, missing key,
    malformed TOML, permission denied) so that a single corrupted file never
    prevents the CLI from starting — the metadata fallback is always available.
    """
    try:
        return _read_pyproject_version()
    except Exception:  # noqa: BLE001 — broad catch is intentional: fallback to metadata
        return metadata.version(PROG)


def _read_pyproject_version() -> str:
    """Read `[project].version` from the nearest `pyproject.toml`.

    Walks up the directory tree from this file to locate `pyproject.toml`.
    Uses ``tomllib`` (stdlib) directly — independent of ``scripts.release_check``
    so that the installed runtime package (``app*``) never imports ``scripts/``,
    which is not included in the wheel.

    Raises a clear error if it cannot be found or does not declare a version.
    """
    for directory in Path(__file__).resolve().parents:
        candidate = directory / "pyproject.toml"
        if candidate.is_file():
            with open(candidate, "rb") as fh:
                data = tomllib.load(fh)
            return data["project"]["version"]
    raise RuntimeError("unable to determine version: pyproject.toml not found")


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser for the CLI executable.

    The parser is configured so that:
    - `--version` prints the version from `get_version()` and exits with code 0
      (handled by argparse's `version` action).
    - `--help`/`-h` prints usage and exits with code 0 (argparse default).
    - `--env-file PATH` stores the given path into `CliOptions.env_file`.
    - Unknown options/arguments cause argparse to write usage to stderr and exit
      with code 2.
    """
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Telegram interactive remote CLI execution gateway",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=get_version(),
    )
    parser.add_argument(
        "--env-file",
        dest="env_file",
        metavar="PATH",
        default=None,
        help="path to an env file (.env) to load configuration from",
    )
    return parser


def parse_cli(argv: list[str] | None = None) -> CliOptions:
    """Parse `argv` into a `CliOptions`.

    `--version`/`--help`/unknown arguments are handled by argparse, which raises
    `SystemExit` directly (exit code 0 for informational output, 2 for invalid
    arguments). This function does not catch `SystemExit`.
    """
    parser = build_arg_parser()
    namespace = parser.parse_args(argv)
    return CliOptions(env_file=namespace.env_file)


async def run(settings: Settings) -> None:
    configure_logging()
    container = AppContainer(settings=settings)
    try:
        container.wire()
        await container.start()

        logger.info("bot starting with polling")

        while True:
            try:
                await container.dispatcher.start_polling(container.bot)
                return
            except TelegramNetworkError as exc:
                logger.warning(
                    "telegram network error, will retry",
                    extra={"error": str(exc), "retry_delay_sec": settings.tg_polling_retry_delay_sec},
                )
                await asyncio.sleep(settings.tg_polling_retry_delay_sec)
    finally:
        await container.stop()


def main() -> None:
    options = parse_cli()
    try:
        settings = load_settings(options.env_file)
    except StartupError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    check_tmux_preflight(settings)
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
