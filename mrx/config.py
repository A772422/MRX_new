"""Runtime configuration, read from the environment (and a .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

AUTONOMY_MODES = ("strict", "balanced", "trusted")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Config:
    home: Path = field(default_factory=lambda: Path(_env("MRX_HOME", "~/.mrx")).expanduser())
    model: str = field(default_factory=lambda: _env("MRX_MODEL", "claude-opus-5-5"))
    effort: str = field(default_factory=lambda: _env("MRX_EFFORT", "medium"))
    autonomy: str = field(default_factory=lambda: _env("MRX_AUTONOMY", "balanced"))
    host: str = field(default_factory=lambda: _env("MRX_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("MRX_PORT", "8765")))
    max_tokens: int = 16000
    max_agent_steps: int = 40
    tool_result_chars: int = 20000
    # Conversation is rolled into a summary and restarted past this many characters.
    max_context_chars: int = 400_000
    confirm_timeout_s: int = 180

    def __post_init__(self) -> None:
        if self.autonomy not in AUTONOMY_MODES:
            raise ValueError(f"MRX_AUTONOMY must be one of {AUTONOMY_MODES}, got {self.autonomy!r}")
        self.home.mkdir(parents=True, exist_ok=True)

    @property
    def db_path(self) -> Path:
        return self.home / "mrx.db"

    @property
    def trash_dir(self) -> Path:
        d = self.home / "trash"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def token_path(self) -> Path:
        return self.home / "token"

    def get(self, name: str, default: str = "") -> str:
        return _env(name, default)
