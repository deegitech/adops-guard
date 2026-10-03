from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from adops_guard.changes import Change, execute
from adops_guard.config import Settings
from adops_guard.context import Context
from adops_guard.output import Output
from adops_guard.state import Journal, WriteLock


class ExecuteTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_dir = Path(tmp.name) / "state"
        self.ctx = Context(
            settings=Settings(state_dir=self.state_dir),
            out=Output(stdout=io.StringIO(), stderr=io.StringIO()),
            env={},
        )
        self.value = 1

    def change(self, **over) -> Change:  # type: ignore[no-untyped-def]
        def apply() -> dict:
            self.value = 2
            return {"ok": True}

        base = dict(
            platform="test", action="thing.set", target="t1", description="1 -> 2", request={"value": 2},
            validate=lambda: None, apply=apply, read_back=lambda: self.value, expected=2,
        )  # fmt: skip
        base.update(over)
        return Change(**base)

    def phases(self) -> list[str]:
        return [e["phase"] for e in Journal(self.state_dir / "journal.jsonl", "x").entries()]

    def test_an_unexpected_error_during_apply_is_journaled_as_ambiguous(self) -> None:
        def apply() -> None:
            raise RuntimeError("connection dropped half-way")

        with self.assertRaises(RuntimeError):
            execute(self.ctx, self.change(apply=apply), apply_flag=True)
        self.assertEqual(["intent", "ambiguous"], self.phases())
        self.assertTrue(self.ctx.store().load()["pending"], "the pending marker stays until a re-run settles it")

    def test_on_success_runs_while_the_lock_is_held(self) -> None:
        held = []

        def on_success() -> dict:
            held.append(bool(WriteLock._depth.get(str((self.state_dir / "lock").resolve()))))
            self.ctx.store().update(lambda d: d.__setitem__("created", True))
            return {"extra": 1}

        self.assertEqual(0, execute(self.ctx, self.change(on_success=on_success), apply_flag=True))
        self.assertEqual([True], held)
        self.assertTrue(self.ctx.store().load()["created"])
        self.assertEqual(["intent", "applied", "verified"], self.phases())

    def test_dry_run_without_platform_validation_sends_nothing(self) -> None:
        calls = []
        change = self.change(validate=lambda: calls.append("validate"), validate_in_dry_run=False)
        self.assertEqual(0, execute(self.ctx, change, apply_flag=False))
        self.assertEqual([], calls)
        self.assertFalse(self.state_dir.exists(), "a dry run must not create the state directory")


if __name__ == "__main__":
    unittest.main()
