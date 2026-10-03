from __future__ import annotations

import json
import pickle
import unittest

from adops_guard.errors import ApiError
from adops_guard.redact import REDACTOR, Secret, redact, redact_data
from fakes import ACCESS_TOKEN, CLIENT_SECRET, META_TOKEN, REFRESH_TOKEN


class SecretTests(unittest.TestCase):
    def tearDown(self) -> None:
        REDACTOR.forget_all()

    def test_secret_never_shows_its_value(self) -> None:
        secret = Secret("a-long-secret-value-123")
        for text in (str(secret), repr(secret), f"{secret}", f"{secret!s}", format(secret, ">40")):
            self.assertNotIn("a-long-secret-value-123", text)
        self.assertEqual("a-long-secret-value-123", secret.reveal())

    def test_secret_refuses_json_and_pickle(self) -> None:
        secret = Secret("another-long-secret-456")
        with self.assertRaises(TypeError):
            json.dumps({"token": secret})
        with self.assertRaises(TypeError):
            pickle.dumps(secret)

    def test_secret_registers_with_the_redactor(self) -> None:
        Secret("registered-secret-789")
        self.assertEqual("token was *** here", redact("token was registered-secret-789 here"))

    def test_error_messages_are_redacted(self) -> None:
        Secret("echoed-back-secret-000")
        error = ApiError("the API echoed echoed-back-secret-000 in its message")
        self.assertNotIn("echoed-back-secret-000", str(error))


class PatternTests(unittest.TestCase):
    def test_known_token_shapes(self) -> None:
        for token in (ACCESS_TOKEN, REFRESH_TOKEN, CLIENT_SECRET, META_TOKEN):
            self.assertNotIn(token, redact(f"value: {token} end"))

    def test_key_value_pairs(self) -> None:
        value = "sample" + "-value-" + "0042"  # built at run time: no secret-shaped literal in the source
        templates = (
            "access_token={v}&x=1",
            '{{"refresh_token": "{v}"}}',
            "'client_secret': '{v}'",
            "developer-token: {v}",
            "Authorization: Bearer {v}",
        )
        for template in templates:
            text = template.format(v=value)
            self.assertNotIn(value, redact(text), text)

    def test_redact_data_masks_secrets_in_keys_too(self) -> None:
        secret = Secret("key-shaped-secret-value-31")
        data = redact_data({secret.reveal(): {"note": secret.reveal()}, "access_token": "x"})
        self.assertNotIn("key-shaped-secret-value-31", json.dumps(data))
        self.assertEqual({"***": {"note": "***"}, "access_token": "***"}, data)

    def test_ordinary_text_is_left_alone(self) -> None:
        text = "Campaign 11111111111: daily budget 30.00 -> 25.00 USD"
        self.assertEqual(text, redact(text))

    def test_redact_data_masks_sensitive_keys_deeply(self) -> None:
        data = {"outer": [{"access_token": "x" * 20, "name": "ok"}], "Authorization": "Bearer y", "n": 5}
        clean = redact_data(data)
        self.assertEqual("***", clean["outer"][0]["access_token"])
        self.assertEqual("ok", clean["outer"][0]["name"])
        self.assertEqual("***", clean["Authorization"])
        self.assertEqual(5, clean["n"])


if __name__ == "__main__":
    unittest.main()
