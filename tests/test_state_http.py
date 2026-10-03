from __future__ import annotations

import gc
import io
import json
import os
import socket
import stat
import tempfile
import threading
import unittest
import warnings
from pathlib import Path

from adops_guard import http
from adops_guard.errors import AdopsError, ConfigError, LockBusy, NetworkError
from adops_guard.output import Output
from adops_guard.redact import REDACTOR, Secret
from adops_guard.state import Journal, StateStore, WriteLock, ensure_private_dir
from mock_http import MockServer

POSIX = os.name != "nt"


class Opaque:
    """A value json.dumps cannot encode, whose repr carries a secret."""

    def __init__(self, text: str) -> None:
        self.text = text

    def __repr__(self) -> str:
        return f"Opaque({self.text})"

    __str__ = __repr__


class StateTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(REDACTOR.forget_all)
        self.dir = ensure_private_dir(Path(tmp.name) / "state")

    @unittest.skipUnless(POSIX, "POSIX permissions")
    def test_directory_and_files_are_private(self) -> None:
        self.assertEqual(0o700, stat.S_IMODE(self.dir.stat().st_mode))
        Journal(self.dir / "journal.jsonl", "run1").record(phase="intent")
        StateStore(self.dir / "state.json").save({"a": 1})
        for name in ("journal.jsonl", "state.json"):
            self.assertEqual(0o600, stat.S_IMODE((self.dir / name).stat().st_mode), name)

    def test_journal_appends_redacted_lines(self) -> None:
        secret = Secret("journal-secret-value-1")
        journal = Journal(self.dir / "journal.jsonl", "run1")
        journal.record(phase="intent", request={"access_token": "abc", "note": f"x {secret.reveal()} y"})
        journal.record(phase="applied", response={"ok": True})
        text = (self.dir / "journal.jsonl").read_text()
        self.assertNotIn("journal-secret-value-1", text)
        entries = journal.entries()
        self.assertEqual(["intent", "applied"], [e["phase"] for e in entries])
        self.assertEqual("***", entries[0]["request"]["access_token"])
        self.assertEqual("run1", entries[1]["run"])

    def test_state_store_round_trip_and_corruption(self) -> None:
        store = StateStore(self.dir / "state.json")
        self.assertEqual({}, store.load())
        store.update(lambda d: d.setdefault("watch", {}).__setitem__("k", {"applied_at": "now"}))
        self.assertEqual({"watch": {"k": {"applied_at": "now"}}}, store.load())
        self.assertEqual([], [p.name for p in self.dir.iterdir() if p.name.endswith(".tmp")])
        (self.dir / "state.json").write_text("{not json")
        with self.assertRaises(Exception) as ctx:
            store.load()
        self.assertIn("not valid JSON", str(ctx.exception))

    def test_values_json_cannot_encode_are_redacted_too(self) -> None:
        secret = Secret("opaque-secret-value-77")
        Journal(self.dir / "journal.jsonl", "run1").record(phase="intent", request={"x": Opaque(secret.reveal())})
        StateStore(self.dir / "state.json").save({"x": Opaque(secret.reveal()), secret.reveal(): 1})
        for name in ("journal.jsonl", "state.json"):
            text = (self.dir / name).read_text()
            self.assertNotIn("opaque-secret-value-77", text, name)
            self.assertIn("Opaque(***)", text, name)
        stdout = io.StringIO()
        Output(json_mode=True, stdout=stdout, stderr=io.StringIO()).emit({"x": Opaque(secret.reveal())})
        self.assertEqual({"x": "Opaque(***)"}, json.loads(stdout.getvalue()))

    @unittest.skipUnless(POSIX, "POSIX permissions")
    def test_an_existing_state_dir_must_be_private(self) -> None:
        shared = self.dir.parent / "shared"
        shared.mkdir()
        os.chmod(shared, 0o777)
        with self.assertRaises(AdopsError) as ctx:
            ensure_private_dir(shared)
        self.assertIn("chmod 700", ctx.exception.hint or "")
        os.chmod(shared, 0o755)  # readable by others is fine: the files inside are 0600
        self.assertEqual(shared, ensure_private_dir(shared))

    @unittest.skipUnless(POSIX, "symlinks")
    def test_a_symlinked_state_dir_is_refused(self) -> None:
        link = self.dir.parent / "link"
        link.symlink_to(self.dir, target_is_directory=True)
        with self.assertRaises(AdopsError):
            ensure_private_dir(link)

    @unittest.skipUnless(POSIX, "symlinks")
    def test_journal_and_lock_never_write_through_a_symlink(self) -> None:
        victim = self.dir.parent / "victim.txt"
        victim.write_text("keep me\n")
        (self.dir / "journal.jsonl").symlink_to(victim)
        (self.dir / "lock").symlink_to(victim)
        with self.assertRaises(AdopsError):
            Journal(self.dir / "journal.jsonl", "run1").record(phase="intent")
        with self.assertRaises(AdopsError):
            with WriteLock(self.dir / "lock", "test"):
                pass
        self.assertEqual("keep me\n", victim.read_text())

    @unittest.skipUnless(POSIX, "hard links")
    def test_hard_linked_journal_is_refused(self) -> None:
        victim = self.dir / "victim.txt"
        victim.write_text("keep me\n")
        os.link(victim, self.dir / "journal.jsonl")
        with self.assertRaises(AdopsError):
            Journal(self.dir / "journal.jsonl", "run1").record(phase="intent")
        self.assertEqual("keep me\n", victim.read_text())

    @unittest.skipUnless(POSIX, "flock")
    def test_lock_is_exclusive_across_open_files_and_reentrant_in_process(self) -> None:
        import fcntl

        path = self.dir / "lock"
        with WriteLock(path, "outer"):
            with WriteLock(path, "inner"):  # re-entrant: the same process may nest
                pass
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # someone else holds it
            with self.assertRaises(LockBusy):
                with WriteLock(path, "blocked"):
                    pass
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        with WriteLock(path, "free again"):
            pass


class HttpTests(unittest.TestCase):
    def test_endpoint_allowlist(self) -> None:
        hosts = ("googleads.googleapis.com",)
        self.assertEqual(
            "https://googleads.googleapis.com", http.check_endpoint("https://googleads.googleapis.com/", hosts)
        )
        self.assertEqual(
            "http://127.0.0.1:8080", http.check_endpoint("http://127.0.0.1:8080", hosts, allow_loopback=True)
        )
        for bad in ("http://googleads.googleapis.com", "https://evil.example", "https://googleads.googleapis.com.evil.example",
                    "https://user:pw@googleads.googleapis.com", "http://127.0.0.1:8080", "https://localhost/"):  # fmt: skip
            with self.assertRaises(ConfigError, msg=bad):
                http.check_endpoint(bad, hosts)
        for bad in ("ftp://127.0.0.1/", "http://evil.example", "https://googleads.googleapis.com.evil.example"):
            with self.assertRaises(ConfigError, msg=bad):
                http.check_endpoint(bad, hosts, allow_loopback=True)

    def test_error_responses_are_closed_at_once(self) -> None:
        server = MockServer(lambda req: (503, {}, {"error": {"message": "busy"}})).start()
        self.addCleanup(server.stop)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            resp = http.request("GET", server.url + "/x", timeout=5)
            del resp
            gc.collect()
        self.assertEqual([], [str(w.message) for w in caught if issubclass(w.category, ResourceWarning)])

    def test_a_truncated_body_is_a_network_error(self) -> None:
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)

        def serve() -> None:
            conn, _ = listener.accept()
            with conn:
                request = b""
                while b"\r\n\r\n" not in request or not request.endswith(b"{}"):  # read the whole request
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    request += chunk
                # The answer promises 100 bytes and sends 4: http.client raises IncompleteRead (not an OSError).
                conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{"a"')

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        port = listener.getsockname()[1]
        with self.assertRaises(NetworkError) as ctx:
            http.request("POST", f"http://127.0.0.1:{port}/v25/x", data=b"{}", timeout=5)
        self.assertIn("IncompleteRead", str(ctx.exception))
        thread.join(5)

    def test_redirects_are_not_followed(self) -> None:
        def app(req):  # type: ignore[no-untyped-def]
            if req.path == "/start":
                return 302, {"Location": "/elsewhere"}, {}
            return 200, {}, {"reached": True}

        server = MockServer(app).start()
        self.addCleanup(server.stop)
        resp = http.request("GET", server.url + "/start", headers={"Authorization": "Bearer x"})
        self.assertEqual(302, resp.status)
        self.assertEqual(["/start"], [r.path for r in server.requests])

    def test_network_errors_raise(self) -> None:
        server = MockServer(lambda req: (200, {}, {})).start()
        url = server.url
        server.stop()
        with self.assertRaises(NetworkError):
            http.request("GET", url + "/x", timeout=2)


if __name__ == "__main__":
    unittest.main()
