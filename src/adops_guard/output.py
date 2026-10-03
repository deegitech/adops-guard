"""Human and JSON output.

Human output is plain ASCII-friendly text (it ends up in cron mail, systemd
journals and CI logs) and is always redacted. With ``--json``, stdout carries
exactly one JSON document, also when the command fails (then it is
``{"error": ..., "hint": ..., "exit_code": ...}``), and the human lines go to
stderr.
"""

from __future__ import annotations

import contextlib
import json
import sys
from collections.abc import Iterable, Iterator, Sequence
from decimal import Decimal
from typing import Any, TextIO

from adops_guard.redact import redact, redact_data


def fmt_int(value: Any) -> str:
    try:
        return f"{int(value or 0):,}"
    except (TypeError, ValueError):
        return str(value)


def fmt_pct(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value * 100:.{digits}f}%"


def fmt_ratio(value: float | None) -> str:
    if value is None:
        return "-"
    if value == float("inf"):
        return "inf"
    return f"{value:.1f}x"


def _json_default(value: Any) -> Any:
    # json.dumps calls this *after* redact_data has run, so the string made here is redacted too.
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return redact(str(value))


class Output:
    def __init__(self, *, json_mode: bool = False, stdout: TextIO | None = None, stderr: TextIO | None = None) -> None:
        self.json_mode = json_mode
        self._out = stdout if stdout is not None else sys.stdout
        self._err = stderr if stderr is not None else sys.stderr
        self.emitted = False
        self._nested = 0

    @contextlib.contextmanager
    def nested(self) -> Iterator[None]:
        """Suppress ``emit`` from sub-steps so the caller can emit one combined document."""
        self._nested += 1
        try:
            yield
        finally:
            self._nested -= 1

    def human_stream(self) -> TextIO:
        """Where :meth:`line` writes: stdout, or stderr in ``--json`` mode."""
        return self._err if self.json_mode else self._out

    def line(self, text: str = "") -> None:
        print(redact(text), file=self.human_stream())

    def warn(self, text: str) -> None:
        print(redact("WARN     " + text), file=self._err)

    def note(self, text: str) -> None:
        """A line about the tool itself (which config is used), always on stderr."""
        print(redact(text), file=self._err)

    def error(self, text: str) -> None:
        print(redact("error: " + text), file=self._err)

    def emit_error(self, message: str, hint: str | None, exit_code: int) -> None:
        """In ``--json`` mode, put the error on stdout too, unless a document was already printed."""
        if self.json_mode and not self.emitted:
            self.emitted = True
            data = {"error": redact(message), "hint": redact(hint) if hint else None, "exit_code": int(exit_code)}
            print(json.dumps(data, indent=2, ensure_ascii=False), file=self._out)

    def table(
        self,
        headers: Sequence[str],
        rows: Iterable[Sequence[Any]],
        *,
        numeric: Iterable[int] = (),
        indent: str = "  ",
    ) -> None:
        cells = [[str(h) for h in headers]] + [["" if c is None else str(c) for c in row] for row in rows]
        right = set(numeric)
        widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]
        for row in cells:
            parts = [row[i].rjust(widths[i]) if i in right else row[i].ljust(widths[i]) for i in range(len(headers))]
            self.line((indent + "  ".join(parts)).rstrip())

    def emit(self, data: Any) -> None:
        """Print the JSON document for ``--json`` (no-op in human mode)."""
        if self.json_mode and not self.emitted and not self._nested:
            self.emitted = True
            text = json.dumps(redact_data(data), indent=2, ensure_ascii=False, default=_json_default)
            print(text, file=self._out)
