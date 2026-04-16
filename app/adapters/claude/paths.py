from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class ClaudePaths:
    root_dir: Path

    @classmethod
    def resolve(
        cls,
        config_dir: str | None = None,
        *,
        env: Mapping[str, str] | None = None,
        home: Path | None = None,
    ) -> "ClaudePaths":
        env_map = env or os.environ
        home_dir = home or Path.home()

        if config_dir:
            return cls(Path(config_dir).expanduser())

        env_dir = (env_map.get("CLAUDE_CONFIG_DIR") or "").strip()
        if env_dir:
            return cls(Path(env_dir).expanduser())

        new_default = home_dir / ".config" / "claude"
        if (new_default / "projects").exists():
            return cls(new_default)

        return cls(home_dir / ".claude")

    @property
    def hooks_dir(self) -> Path:
        return self.root_dir / "hooks"

    @property
    def settings_file(self) -> Path:
        return self.root_dir / "settings.json"

    @property
    def projects_dir(self) -> Path:
        return self.root_dir / "projects"

    def hook_script_path(self, script_name: str) -> Path:
        return self.hooks_dir / script_name
