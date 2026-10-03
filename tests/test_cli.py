from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from adops_guard import __version__
from adops_guard.cli import build_parser, main
from adops_guard.redact import Secret
from fakes import META_TOKEN, Harness


def all_option_strings(parser: argparse.ArgumentParser) -> list[str]:
    found = []
    for action in parser._actions:
        found += list(action.option_strings)
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                found += all_option_strings(sub)
    return found


class CliTests(unittest.TestCase):
    def test_version_and_help(self) -> None:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            self.assertEqual(0, main(["--version"]))
        self.assertIn(__version__, out.getvalue())
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(0, main(["google", "--help"]))

    def test_usage_errors_exit_2(self) -> None:
        with mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(2, main(["google", "nope"]))
            self.assertEqual(2, main([]))
            self.assertEqual(2, main(["google", "pause", "--campaign", "1", "--ad-group", "2"]))

    def test_no_option_accepts_a_secret(self) -> None:
        options = all_option_strings(build_parser())
        self.assertTrue(options)
        for option in options:
            for word in ("token", "secret", "password", "key"):
                self.assertNotIn(word, option, f"{option} would put a secret in argv")

    def test_module_entry_point(self) -> None:
        done = subprocess.run(
            [sys.executable, "-m", "adops_guard", "--version"], capture_output=True, text=True, check=False
        )
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn(__version__, done.stdout)

    def test_config_with_a_secret_is_refused(self) -> None:
        h = Harness(self)
        h.config["google"]["developer_token"] = "x"
        result = h.run("google", "status")
        self.assertEqual(2, result.code)
        self.assertIn("looks like a secret", result.err)

    def test_unexpected_errors_are_redacted(self) -> None:
        h = Harness(self)
        h.write_config()
        Secret(META_TOKEN)

        def boom(ctx, args):  # type: ignore[no-untyped-def]
            raise RuntimeError(f"something failed near {META_TOKEN}")

        class StubParser:
            def parse_args(self, argv):  # type: ignore[no-untyped-def]
                return argparse.Namespace(func=boom, json=False, debug=True, config=str(h.config_path), state_dir=None)

        err = io.StringIO()
        with mock.patch("adops_guard.cli.build_parser", return_value=StubParser()):
            code = main(["meta", "whoami"], env=h.env, stdout=io.StringIO(), stderr=err, cwd=h.dir)
        self.assertEqual(1, code)
        self.assertIn("unexpected RuntimeError", err.getvalue())
        self.assertIn("Traceback", err.getvalue())
        self.assertNotIn(META_TOKEN, err.getvalue())

    def test_state_dir_override(self) -> None:
        h = Harness(self)
        h.google.add_campaign("11111111111", budget_micros=30_000_000)
        other = h.dir / "elsewhere"
        result = h.run(
            "google",
            "budget",
            "set",
            "--campaign",
            "11111111111",
            "--amount",
            "20",
            "--apply",
            "--state-dir",
            str(other),
        )
        self.assertEqual(0, result.code, result.err)
        self.assertTrue((other / "journal.jsonl").exists())
        self.assertFalse(h.state_dir.exists())

    def test_json_errors_are_one_document_on_stdout(self) -> None:
        h = Harness(self)
        h.google.add_campaign("11111111111", budget_micros=30_000_000)
        result = h.run("google", "budget", "set", "--campaign", "11111111111", "--amount", "80", json_mode=True)
        self.assertEqual(3, result.code)
        data = result.json()
        self.assertEqual(3, data["exit_code"])
        self.assertIn("above the configured ceiling", data["error"])
        self.assertIn("--override-limit", data["hint"])
        out = io.StringIO()
        with mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(2, main(["--json", "google", "nope"], stdout=out))
        self.assertEqual(2, json.loads(out.getvalue())["exit_code"])

    def test_loopback_endpoints_need_the_test_flag(self) -> None:
        h = Harness(self)
        result = h.run("google", "status")
        self.assertIn("a test endpoint (ADOPS_GUARD_TEST_ENDPOINTS=1)", result.err)  # said out loud when used
        del h.env["ADOPS_GUARD_TEST_ENDPOINTS"]
        sent = len(h.server.requests)
        result = h.run("google", "status")
        self.assertEqual(2, result.code)
        self.assertIn("only for the offline test suite", result.err)
        self.assertEqual(sent, len(h.server.requests), "no request may reach a non-official endpoint")

    def test_a_config_found_in_the_working_directory_is_announced(self) -> None:
        h = Harness(self)
        h.config = {"google": {"customer_id": "123-456-7890"}}
        h.write_config()
        err = io.StringIO()
        with mock.patch("sys.stdout", io.StringIO()):
            main(["meta", "video-check"], env=h.env, stdout=io.StringIO(), stderr=err, cwd=h.dir)
        self.assertIn(f"using config {h.config_path} (found in the working directory)", err.getvalue())
        if os.name != "nt":
            os.chmod(h.config_path, 0o666)
            err = io.StringIO()
            code = main(["meta", "video-check"], env=h.env, stdout=io.StringIO(), stderr=err, cwd=h.dir)
            self.assertEqual(2, code)
            self.assertIn("can be changed by other users", err.getvalue())

    def test_json_mode_keeps_stdout_clean(self) -> None:
        h = Harness(self)
        h.google.add_campaign("11111111111", budget_micros=30_000_000)
        result = h.run("google", "budget", "set", "--campaign", "11111111111", "--amount", "20", json_mode=True)
        self.assertEqual(0, result.code, result.err)
        data = result.json()
        self.assertFalse(data["applied"])
        self.assertIn("PLAN", result.err)


if __name__ == "__main__":
    unittest.main()
