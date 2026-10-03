from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from adops_guard.config import GoogleSettings, MetaSettings, load_settings
from adops_guard.credentials import load_google_credentials, load_meta_token, parse_flat_yaml
from adops_guard.errors import ConfigError, CredentialError
from adops_guard.redact import REDACTOR
from fakes import CLIENT_ID, CLIENT_SECRET, DEV_TOKEN, META_TOKEN, REFRESH_TOKEN

POSIX = os.name != "nt"


class TempDirTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(REDACTOR.forget_all)
        self.dir = Path(tmp.name)

    def write(self, name: str, text: str, mode: int = 0o600) -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        os.chmod(path, mode)
        return path


class ConfigTests(TempDirTest):
    def test_no_file_gives_defaults(self) -> None:
        settings = load_settings(None, {}, self.dir)
        self.assertIsNone(settings.source)
        self.assertEqual("v25", settings.google.api_version)
        self.assertEqual(self.dir / ".adops-guard", settings.state_dir)
        self.assertIsNone(settings.google.max_daily_budget)

    def test_reads_values_and_resolves_state_dir_next_to_the_file(self) -> None:
        sub = self.dir / "conf"
        sub.mkdir()
        path = sub / "adops-guard.ini"
        path.write_text(
            "[general]\nstate_dir = journal\n"
            "[google]\ncustomer_id = 123-456-7890\nmax_daily_budget = 50.00  # ceiling\n"
            "[meta]\nad_account_id = act_123456789012345\ntoken_source = env\nmax_spend_cap = 1000\n"
            "[audit]\nnight_hours = 22-5\nextra_kids_keywords = bebe, nene\n",
            encoding="utf-8",
        )
        os.chmod(path, 0o600)  # whatever the umask: a group-writable config would be refused
        settings = load_settings(str(path), {}, self.dir)
        self.assertEqual(Decimal("50.00"), settings.google.max_daily_budget)
        self.assertEqual(Decimal("1000"), settings.meta.max_spend_cap)
        self.assertEqual(sub / "journal", settings.state_dir)
        self.assertEqual((22, 5), settings.audit.night_hours)
        self.assertEqual(("bebe", "nene"), settings.audit.extra_kids_keywords)

    def test_finds_the_file_in_the_working_directory_and_env(self) -> None:
        self.write("adops-guard.ini", "[google]\ncustomer_id = 1234567890\n")
        found = load_settings(None, {}, self.dir)
        self.assertEqual("1234567890", found.google.customer_id)
        self.assertTrue(found.discovered)
        other = self.write("other.ini", "[google]\ncustomer_id = 2234567890\n")
        named = load_settings(None, {"ADOPS_GUARD_CONFIG": str(other)}, self.dir)
        self.assertEqual("2234567890", named.google.customer_id)
        self.assertFalse(named.discovered)

    def test_test_endpoints_need_the_environment_flag(self) -> None:
        for section, key, url in (("google", "api_base_url", "http://127.0.0.1:9"),
                                  ("google", "oauth_token_url", "http://127.0.0.1:9/token"),
                                  ("meta", "graph_base_url", "http://127.0.0.1:9")):  # fmt: skip
            path = self.write("adops-guard.ini", f"[{section}]\n{key} = {url}\n")
            with self.assertRaises(ConfigError, msg=key) as ctx:
                load_settings(None, {}, self.dir)  # e.g. a file planted in a checkout you run the tool from
            self.assertIn("ADOPS_GUARD_TEST_ENDPOINTS=1", ctx.exception.hint or "")
            settings = load_settings(str(path), {"ADOPS_GUARD_TEST_ENDPOINTS": "1"}, self.dir)
            self.assertTrue(settings.test_endpoints)
            self.assertEqual([(f"[{section}] {key}", url)], settings.endpoint_overrides())
        self.assertEqual([], load_settings(None, {}, self.dir / "nowhere").endpoint_overrides())

    @unittest.skipUnless(POSIX, "POSIX permissions")
    def test_config_files_others_can_change_are_refused(self) -> None:
        for mode in (0o664, 0o646, 0o666):
            path = self.write("adops-guard.ini", "[google]\ncustomer_id = 1234567890\n", mode=mode)
            with self.assertRaises(ConfigError, msg=oct(mode)) as ctx:
                load_settings(str(path), {}, self.dir)
            self.assertIn("chmod go-w", ctx.exception.hint or "")
        path = self.write("adops-guard.ini", "[google]\ncustomer_id = 1234567890\n", mode=0o644)
        self.assertEqual("1234567890", load_settings(str(path), {}, self.dir).google.customer_id)

    def test_unknown_keys_and_sections_are_errors(self) -> None:
        path = self.write("a.ini", "[google]\nmax_daily_budjet = 50\n")
        with self.assertRaises(ConfigError):
            load_settings(str(path), {}, self.dir)
        path = self.write("b.ini", "[googel]\ncustomer_id = 1234567890\n")
        with self.assertRaises(ConfigError):
            load_settings(str(path), {}, self.dir)

    def test_secrets_are_refused_in_the_config_file(self) -> None:
        for key in ("developer_token", "refresh_token", "client_secret", "access_token", "password"):
            path = self.write(f"{key}.ini", f"[google]\n{key} = whatever\n")
            with self.assertRaises(ConfigError) as ctx:
                load_settings(str(path), {}, self.dir)
            self.assertIn("looks like a secret", str(ctx.exception))

    def test_bad_values(self) -> None:
        for text in ("[google]\nmax_daily_budget = 1e9\n", "[meta]\ntoken_source = clipboard\n",
                     "[audit]\nnight_hours = late\n", "[meta]\nusage_stop_percent = 150\n"):  # fmt: skip
            path = self.write("bad.ini", text)
            with self.assertRaises(ConfigError, msg=text):
                load_settings(str(path), {}, self.dir)

    def test_missing_explicit_file(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            load_settings(str(self.dir / "nope.ini"), {}, self.dir)
        self.assertIn("adops-guard.example.ini", ctx.exception.hint or "")

    def test_a_pasted_second_block_names_the_fix(self) -> None:
        # What happens when a setup step's [meta] block is pasted below the copied example.
        for text in ("[meta]\nmax_daily_budget = 50\n[meta]\ntoken_source = keychain\n",
                     "[meta]\ntoken_source = env\ntoken_source = keychain\n"):  # fmt: skip
            path = self.write("dup.ini", text)
            with self.assertRaises(ConfigError, msg=text) as ctx:
                load_settings(str(path), {}, self.dir)
            self.assertIn("already exists", str(ctx.exception))
            self.assertIn("only once", ctx.exception.hint or "")
        path = self.write("broken.ini", "max_daily_budget = 50\n")  # no [section] header
        with self.assertRaises(ConfigError) as ctx:
            load_settings(str(path), {}, self.dir)
        self.assertIn("[section] headers", ctx.exception.hint or "")


class GoogleCredentialTests(TempDirTest):
    def env(self, **extra: str) -> dict[str, str]:
        base = {
            "GOOGLE_ADS_DEVELOPER_TOKEN": DEV_TOKEN,
            "GOOGLE_ADS_CLIENT_ID": CLIENT_ID,
            "GOOGLE_ADS_CLIENT_SECRET": CLIENT_SECRET,
            "GOOGLE_ADS_REFRESH_TOKEN": REFRESH_TOKEN,
        }
        base.update(extra)
        return base

    def yaml(self) -> str:
        return (
            "# google-ads.yaml\n"
            f"developer_token: {DEV_TOKEN}\n"
            f"client_id: '{CLIENT_ID}'\n"
            f'client_secret: "{CLIENT_SECRET}"\n'
            f"refresh_token: {REFRESH_TOKEN}  # comment\n"
            "login_customer_id: 1234567890\n"
            "use_proto_plus: True\n"
            "nested:\n  ignored: yes\n"
        )

    def test_environment(self) -> None:
        creds = load_google_credentials(GoogleSettings(), self.env(GOOGLE_ADS_LOGIN_CUSTOMER_ID="1234567890"))
        self.assertEqual(REFRESH_TOKEN, creds.refresh_token.reveal())
        self.assertEqual("1234567890", creds.login_customer_id)
        self.assertNotIn(REFRESH_TOKEN, repr(creds))

    def test_partial_environment_names_what_is_missing_without_values(self) -> None:
        env = self.env()
        del env["GOOGLE_ADS_REFRESH_TOKEN"]
        with self.assertRaises(CredentialError) as ctx:
            load_google_credentials(GoogleSettings(), env)
        self.assertIn("GOOGLE_ADS_REFRESH_TOKEN", str(ctx.exception))
        self.assertNotIn(CLIENT_SECRET, str(ctx.exception))

    def test_yaml_file(self) -> None:
        path = self.write("google-ads.yaml", self.yaml())
        creds = load_google_credentials(GoogleSettings(credentials_file=str(path)), {})
        self.assertEqual(DEV_TOKEN, creds.developer_token.reveal())
        self.assertEqual(CLIENT_ID, creds.client_id.reveal())
        self.assertEqual(CLIENT_SECRET, creds.client_secret.reveal())
        self.assertEqual(REFRESH_TOKEN, creds.refresh_token.reveal())
        self.assertEqual("1234567890", creds.login_customer_id)

    def test_yaml_path_from_the_standard_env_var(self) -> None:
        path = self.write("g.yaml", self.yaml())
        creds = load_google_credentials(GoogleSettings(), {"GOOGLE_ADS_CONFIGURATION_FILE_PATH": str(path)})
        self.assertEqual(str(path), creds.source)

    @unittest.skipUnless(POSIX, "POSIX permissions")
    def test_readable_yaml_is_refused(self) -> None:
        path = self.write("google-ads.yaml", self.yaml(), mode=0o644)
        with self.assertRaises(CredentialError) as ctx:
            load_google_credentials(GoogleSettings(credentials_file=str(path)), {})
        self.assertIn("chmod 600", ctx.exception.hint or "")

    def test_placeholders_and_service_accounts(self) -> None:
        path = self.write("p.yaml", "client_id: INSERT_CLIENT_ID_HERE\nclient_secret: y\nrefresh_token: z\n")
        with self.assertRaises(CredentialError):
            load_google_credentials(GoogleSettings(credentials_file=str(path)), {})
        path = self.write("s.yaml", f"developer_token: {DEV_TOKEN}\njson_key_file_path: /path/key.json\n")
        with self.assertRaises(CredentialError) as ctx:
            load_google_credentials(GoogleSettings(credentials_file=str(path)), {})
        self.assertIn("service-account", str(ctx.exception))

    def test_the_developer_token_is_optional(self) -> None:
        text = f"developer_token: INSERT_DEVELOPER_TOKEN_HERE\nclient_id: {CLIENT_ID}\nclient_secret: {CLIENT_SECRET}\n"
        path = self.write("n.yaml", text + f"refresh_token: {REFRESH_TOKEN}\n")
        self.assertIsNone(load_google_credentials(GoogleSettings(credentials_file=str(path)), {}).developer_token)
        env = self.env()
        del env["GOOGLE_ADS_DEVELOPER_TOKEN"]
        self.assertIsNone(load_google_credentials(GoogleSettings(), env).developer_token)

    def test_missing_file(self) -> None:
        with self.assertRaises(CredentialError):
            load_google_credentials(GoogleSettings(credentials_file=str(self.dir / "none.yaml")), {})

    def test_flat_yaml_parser(self) -> None:
        self.assertEqual(
            {"a": "1", "b": "two words", "c": "x # not a comment"},
            parse_flat_yaml("a: 1\nb: two words   # comment\nc: 'x # not a comment'\n  d: nested\n"),
        )


class MetaTokenTests(TempDirTest):
    def test_env(self) -> None:
        token = load_meta_token(MetaSettings(), {"META_ACCESS_TOKEN": META_TOKEN})
        self.assertEqual(META_TOKEN, token.reveal())

    def test_custom_env_name(self) -> None:
        token = load_meta_token(MetaSettings(token_source="env", token_env="MY_TOKEN"), {"MY_TOKEN": META_TOKEN})
        self.assertEqual(META_TOKEN, token.reveal())

    def test_file(self) -> None:
        path = self.write("token", META_TOKEN + "\n")
        self.assertEqual(META_TOKEN, load_meta_token(MetaSettings(token_file=str(path)), {}).reveal())
        self.assertEqual(META_TOKEN, load_meta_token(MetaSettings(), {"META_ACCESS_TOKEN_FILE": str(path)}).reveal())

    @unittest.skipUnless(POSIX, "POSIX permissions")
    def test_world_readable_file_is_refused(self) -> None:
        path = self.write("token", META_TOKEN, mode=0o644)
        with self.assertRaises(CredentialError):
            load_meta_token(MetaSettings(token_file=str(path)), {})

    def test_empty_or_spaced_tokens(self) -> None:
        with self.assertRaises(CredentialError):
            load_meta_token(MetaSettings(), {"META_ACCESS_TOKEN": "  "} | {"META_ACCESS_TOKEN_FILE": ""})
        with self.assertRaises(CredentialError):
            load_meta_token(MetaSettings(token_source="env"), {"META_ACCESS_TOKEN": "two parts"})

    def test_no_source(self) -> None:
        with self.assertRaises(CredentialError) as ctx:
            load_meta_token(MetaSettings(), {})
        self.assertIn("token_source", ctx.exception.hint or "")
        self.assertIn("Meta steps 1 to 6", ctx.exception.hint or "")
        self.assertIn("read -rs META_ACCESS_TOKEN && export META_ACCESS_TOKEN", ctx.exception.hint or "")

    def test_every_token_source_error_has_a_hint(self) -> None:
        def not_found(cmd, **kwargs):  # type: ignore[no-untyped-def]
            raise FileNotFoundError(cmd[0])

        def denied(cmd, **kwargs):  # type: ignore[no-untyped-def]
            return SimpleNamespace(returncode=36, stdout="", stderr="security: SecKeychainSearchCopyNext: denied\n")

        cases = [
            (MetaSettings(token_source="env", token_env="MY_TOKEN"), "read -rs MY_TOKEN && export MY_TOKEN"),
            (MetaSettings(token_source="file"), "token_file = ~/.config/adops-guard/meta-token"),
            (MetaSettings(token_source="file", token_file=str(self.dir / "none")), "Meta step 6"),
            (MetaSettings(token_source="ssm"), "ssm_parameter = /adops-guard/meta-token"),
            (MetaSettings(token_source="ssm", ssm_parameter="/x"), "install the AWS CLI v2"),
        ]
        for settings, words in cases:
            with self.assertRaises(CredentialError, msg=settings) as ctx:
                load_meta_token(settings, {}, not_found)
            self.assertIn(words, ctx.exception.hint or "", settings)
        with mock.patch("adops_guard.credentials.sys.platform", "linux"):
            with self.assertRaises(CredentialError) as ctx:
                load_meta_token(MetaSettings(token_source="keychain", keychain_service="x"), {}, not_found)
        self.assertIn("token_source to file, env or ssm", ctx.exception.hint or "")
        with mock.patch("adops_guard.credentials.sys.platform", "darwin"):
            with self.assertRaises(CredentialError) as ctx:
                load_meta_token(MetaSettings(token_source="keychain"), {}, not_found)
            self.assertIn("keychain_service = adops-guard-meta", ctx.exception.hint or "")
            with self.assertRaises(CredentialError) as ctx:
                load_meta_token(MetaSettings(token_source="keychain", keychain_service="x"), {}, denied)
            self.assertIn("unlocked", ctx.exception.hint or "")

    def test_google_credentials_point_at_the_whole_setup(self) -> None:
        with self.assertRaises(CredentialError) as ctx:
            load_google_credentials(GoogleSettings(credentials_file=str(self.dir / "none.yaml")), {})
        self.assertIn("Google steps 1 to 5", ctx.exception.hint or "")

    def test_keychain_reads_stdout_and_keeps_the_token_out_of_argv(self) -> None:
        calls = []

        def runner(cmd, **kwargs):  # type: ignore[no-untyped-def]
            calls.append((cmd, kwargs))
            return SimpleNamespace(returncode=0, stdout=META_TOKEN + "\n", stderr="")

        settings = MetaSettings(token_source="keychain", keychain_service="adops-guard-meta", keychain_account="me")
        with mock.patch("adops_guard.credentials.sys.platform", "darwin"):
            token = load_meta_token(settings, {}, runner)
        self.assertEqual(META_TOKEN, token.reveal())
        cmd, kwargs = calls[0]
        self.assertEqual(["security", "find-generic-password", "-s", "adops-guard-meta", "-a", "me", "-w"], cmd)
        self.assertNotIn(META_TOKEN, " ".join(cmd))
        self.assertEqual(subprocess.DEVNULL, kwargs["stdin"])
        self.assertTrue(kwargs["timeout"])

    def test_keychain_only_on_macos(self) -> None:
        with mock.patch("adops_guard.credentials.sys.platform", "linux"):
            with self.assertRaises(CredentialError):
                load_meta_token(MetaSettings(token_source="keychain", keychain_service="x"), {}, lambda *a, **k: None)

    def test_ssm(self) -> None:
        calls = []

        def runner(cmd, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout=META_TOKEN, stderr="")

        settings = MetaSettings(token_source="ssm", ssm_parameter="/adops-guard/meta-token", ssm_region="us-east-1")
        self.assertEqual(META_TOKEN, load_meta_token(settings, {}, runner).reveal())
        self.assertIn("--with-decryption", calls[0])
        self.assertEqual(["--region", "us-east-1"], calls[0][-2:])

    def test_helper_failures_are_reported_without_secrets(self) -> None:
        def failing(cmd, **kwargs):  # type: ignore[no-untyped-def]
            return SimpleNamespace(returncode=255, stdout="", stderr="ParameterNotFound\n")

        settings = MetaSettings(token_source="ssm", ssm_parameter="/missing")
        with self.assertRaises(CredentialError) as ctx:
            load_meta_token(settings, {}, failing)
        self.assertIn("ParameterNotFound", str(ctx.exception))

        def missing(cmd, **kwargs):  # type: ignore[no-untyped-def]
            raise FileNotFoundError(cmd[0])

        with self.assertRaises(CredentialError) as ctx:
            load_meta_token(settings, {}, missing)
        self.assertIn("'aws' was not found", str(ctx.exception))

        def slow(cmd, **kwargs):  # type: ignore[no-untyped-def]
            raise subprocess.TimeoutExpired(cmd, 30)

        with self.assertRaises(CredentialError):
            load_meta_token(settings, {}, slow)


class ExampleFileTests(TempDirTest):
    root = Path(__file__).resolve().parents[1]

    def test_example_config_is_valid(self) -> None:
        # A private copy: a checkout made with a group-writable umask must not fail this test.
        text = (self.root / "examples" / "adops-guard.example.ini").read_text(encoding="utf-8")
        settings = load_settings(str(self.write("adops-guard.ini", text)), {}, self.dir)
        # The account ids and the token source stay commented out until the setup steps fill them in, so the
        # doctor skips an untouched platform and a placeholder id is never sent to an API.
        self.assertIsNone(settings.google.customer_id)
        self.assertIsNone(settings.meta.ad_account_id)
        self.assertIsNone(settings.meta.token_source)
        self.assertEqual(Decimal("50.00"), settings.google.max_daily_budget)
        self.assertEqual(Decimal("1000.00"), settings.meta.max_spend_cap)
        self.assertEqual((0, 6), settings.audit.night_hours)
        # Left unset, so $GOOGLE_ADS_CONFIGURATION_FILE_PATH still works for people who copy the example.
        self.assertIsNone(settings.google.credentials_file)
        self.assertEqual([], settings.endpoint_overrides())

    def test_the_setup_steps_edit_the_example_without_duplicates(self) -> None:
        # docs/setup.md: uncomment and set customer_id (Google step 6), the Keychain block (Meta step 6) and
        # ad_account_id (Meta step 7).
        text = (self.root / "examples" / "adops-guard.example.ini").read_text(encoding="utf-8")
        for line in ("customer_id = 123-456-7890", "token_source = keychain",
                     "keychain_service = adops-guard-meta", "ad_account_id = act_123456789012345"):  # fmt: skip
            self.assertIn("# " + line + "\n", text)
            text = text.replace("# " + line + "\n", line + "\n", 1)
        settings = load_settings(str(self.write("adops-guard.ini", text)), {}, self.dir)
        self.assertEqual("123-456-7890", settings.google.customer_id)
        self.assertEqual("keychain", settings.meta.token_source)
        self.assertEqual("adops-guard-meta", settings.meta.keychain_service)
        self.assertEqual("act_123456789012345", settings.meta.ad_account_id)

    def test_example_credentials_file_is_refused_until_filled_in(self) -> None:
        text = (self.root / "examples" / "google-ads.example.yaml").read_text(encoding="utf-8")
        path = self.write("google-ads.yaml", text)
        with self.assertRaises(CredentialError) as ctx:
            load_google_credentials(GoogleSettings(credentials_file=str(path)), {})
        self.assertIn("client_id, client_secret, refresh_token", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
