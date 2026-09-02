"""Abstract process runner and background-handle contracts."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal, Optional

RiskTier = Literal["low", "medium", "high", "core"]  # The caller classifies risk; the runner enforces it.


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class Handle(ABC):
    """Control a detached process."""

    log_path: str

    @abstractmethod
    def poll(self) -> Optional[int]:
        """Return the exit code when complete, otherwise None."""

    @abstractmethod
    def wait(self, timeout: Optional[float] = None) -> int:
        ...

    @abstractmethod
    def kill(self) -> None:
        """Terminate the process and its managed descendants."""


class Runner(ABC):
    @abstractmethod
    def exec(
        self,
        cmd: list[str],
        *,
        cwd: str,
        env: Optional[dict] = None,
        timeout: Optional[float] = None,
        risk: RiskTier = "low",
        worker: bool = False,
    ) -> ExecResult:
        """Execute a command synchronously."""

    @abstractmethod
    def spawn(
        self,
        cmd: list[str],
        *,
        cwd: str,
        log_path: str,
        env: Optional[dict] = None,
        risk: RiskTier = "medium",
        worker: bool = True,
    ) -> Handle:
        """Start a detached command and return its handle."""

    @abstractmethod
    def read_file(self, path: str) -> str:
        ...

    @abstractmethod
    def write_file(self, path: str, content: str) -> None:
        ...

    @abstractmethod
    def exists(self, path: str) -> bool:
        ...
