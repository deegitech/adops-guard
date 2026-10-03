"""Everything a command needs: settings, output, clock, and the state directory."""

from __future__ import annotations

import subprocess
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from adops_guard.config import Settings
from adops_guard.dates import utc_now
from adops_guard.output import Output
from adops_guard.state import Journal, StateStore, WriteLock, ensure_private_dir


@dataclass
class Context:
    settings: Settings
    out: Output
    env: Mapping[str, str]
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], datetime] = utc_now
    monotonic: Callable[[], float] = time.monotonic
    runner: Callable[..., Any] = subprocess.run
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def state_dir(self) -> Path:
        return ensure_private_dir(self.settings.state_dir)

    def journal(self) -> Journal:
        return Journal(self.state_dir() / "journal.jsonl", self.run_id)

    def store(self) -> StateStore:
        return StateStore(self.state_dir() / "state.json")

    def lock(self, what: str) -> WriteLock:
        return WriteLock(self.state_dir() / "lock", what)
