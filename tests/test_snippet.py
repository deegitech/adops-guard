"""Checks for snippets/landing-consent-conversion.html.

The static checks always run. The behaviour checks run the snippet's JavaScript in Node.js with a tiny
fake DOM, and are skipped when ``node`` is not installed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SNIPPET = Path(__file__).resolve().parents[1] / "snippets" / "landing-consent-conversion.html"
EEA_UK_CH = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IS", "IE", "IT",
    "LV", "LI", "LT", "LU", "MT", "NL", "NO", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "GB", "CH",
}  # fmt: skip

FAKE_DOM = r"""
const vm = require('vm');
const [part1, part2, search, stored] = JSON.parse(require('fs').readFileSync(0, 'utf8'));
function storage(init) { const d = Object.assign({}, init); return {
  getItem: k => (k in d ? d[k] : null), setItem: (k, v) => { d[k] = String(v); }, dump: () => d }; }
const listeners = {};
const box = { hidden: true, attrs: {}, addEventListener: (t, f) => { listeners['box:' + t] = f; } };
const privacy = { href: '' };
const links = [
  { href: 'https://apps.apple.com/app/id1234567890' },
  { href: 'https://apps.apple.com/us/app/some-game/id1234567890?l=en' },
  { href: 'https://apps.apple.com/app/id12345678901' },
  { href: 'https://example.com/' },
];
const appended = [];
const document = {
  head: { appendChild: s => appended.push(s.src) },
  createElement: () => ({}),
  getElementById: id => (id === 'ads-consent' ? box : privacy),
  querySelectorAll: () => links.filter(l => l.href.indexOf('apps.apple.com') !== -1),
  addEventListener: (t, f) => { listeners['doc:' + t] = f; },
};
const window = { dataLayer: undefined, location: { search }, localStorage: storage(stored), sessionStorage: storage({}), document };
window.window = window;
const ctx = vm.createContext(Object.assign(window, { URLSearchParams, Date, encodeURIComponent, RegExp }));
vm.runInContext(part1, ctx);
vm.runInContext(part2, ctx);
const dl = () => ctx.dataLayer.map(a => Array.from(a));
const before = dl().length;
const appendedBefore = appended.length;
listeners['doc:click']({ target: { closest: () => links[0] } });
const conversion = dl().slice(before);
listeners['box:click']({ target: { getAttribute: () => 'granted' } });
process.stdout.write(JSON.stringify({ calls: dl(), conversion, links: links.map(l => l.href), hidden: box.hidden,
  appended, appendedBefore, privacy: privacy.href, stored: ctx.localStorage.dump(), session: ctx.sessionStorage.dump() }));
"""


def scripts() -> tuple[str, str]:
    blocks = re.findall(r"<script>(.*?)</script>", SNIPPET.read_text(encoding="utf-8"), re.S)
    return blocks[0], blocks[1]


class StaticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = SNIPPET.read_text(encoding="utf-8")

    def test_defaults_cover_eea_uk_and_switzerland(self) -> None:
        region = re.search(r"region:\s*\[(.*?)\]", self.text, re.S)
        assert region is not None
        self.assertEqual(EEA_UK_CH, set(re.findall(r"'([A-Z]{2})'", region.group(1))))

    def test_denied_defaults_come_before_the_tag_loads(self) -> None:
        denied = self.text.index("ad_storage: 'denied'")
        self.assertLess(denied, self.text.index("googletagmanager.com/gtag/js"))
        self.assertLess(denied, self.text.index("gtag('config'"))
        self.assertEqual(2, self.text.count("ad_personalization: 'denied'"))
        self.assertNotIn("ad_personalization: 'granted'", self.text)

    def test_placeholders_only(self) -> None:
        self.assertEqual(set(), {m for m in re.findall(r"AW-(\d+)", self.text) if set(m) != {"0"}})
        self.assertIn("appStoreAppId: '1234567890'", self.text)
        self.assertIn("providerToken: '000000'", self.text)

    def test_campaign_token_is_validated_and_the_hit_uses_beacon(self) -> None:
        self.assertIn("/^[A-Za-z0-9_-]{1,40}$/", self.text)
        self.assertIn("transport_type: 'beacon'", self.text)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class BehaviourTests(unittest.TestCase):
    def run_snippet(self, search: str = "", stored: dict[str, str] | None = None, mode: str = "advanced") -> dict:
        part1, part2 = scripts()
        part1 = part1.replace("consentMode: 'advanced'", f"consentMode: '{mode}'")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(FAKE_DOM)
        self.addCleanup(Path(handle.name).unlink)
        done = subprocess.run(
            ["node", handle.name],
            input=json.dumps([part1, part2, search, stored or {}]),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(0, done.returncode, done.stderr)
        return json.loads(done.stdout)

    def test_consent_defaults_banner_and_tag(self) -> None:
        result = self.run_snippet()
        calls = result["calls"]
        self.assertEqual(["consent", "default"], calls[0][:2])
        self.assertEqual("denied", calls[0][2]["ad_storage"])
        self.assertEqual(["consent", "default"], calls[1][:2])
        self.assertEqual("granted", calls[1][2]["ad_storage"])
        self.assertIn(["config", "AW-000000000"], [c[:2] for c in calls])
        self.assertEqual(["https://www.googletagmanager.com/gtag/js?id=AW-000000000"], result["appended"])
        self.assertTrue(result["hidden"], "the banner hides after a choice")
        self.assertEqual({"ads-consent": "granted"}, result["stored"])
        self.assertIn(["consent", "update", {"ad_storage": "granted", "ad_user_data": "granted"}], calls)
        self.assertEqual("/privacy/", result["privacy"])

    def test_conversion_on_app_store_click(self) -> None:
        conversion = self.run_snippet()["conversion"]
        self.assertEqual(
            [["event", "conversion", {"send_to": "AW-000000000/REPLACE_WITH_LABEL", "transport_type": "beacon"}]],
            conversion,
        )

    def test_ct_rewrites_only_links_to_this_app(self) -> None:
        links = self.run_snippet(search="?ct=summer-sale_1")["links"]
        expected = "https://apps.apple.com/app/apple-store/id1234567890?pt=000000&ct=summer-sale_1&mt=8"
        self.assertEqual(
            [expected, expected, "https://apps.apple.com/app/id12345678901", "https://example.com/"], links
        )

    def test_bad_ct_values_are_ignored(self) -> None:
        for search in ("?ct=%3Cscript%3E", "?ct=" + "x" * 41, "?ct="):
            links = self.run_snippet(search=search)["links"]
            self.assertEqual("https://apps.apple.com/app/id1234567890", links[0], search)

    def test_basic_mode_loads_nothing_before_consent(self) -> None:
        self.assertEqual(1, self.run_snippet(mode="advanced")["appendedBefore"])
        result = self.run_snippet(mode="basic")
        self.assertEqual(0, result["appendedBefore"], "basic mode must not load the Google tag before OK")
        self.assertEqual(1, len(result["appended"]), "the tag loads once the visitor presses OK")
        self.assertEqual(0, self.run_snippet(mode="basic", stored={"ads-consent": "denied"})["appendedBefore"])
        self.assertEqual(1, self.run_snippet(mode="basic", stored={"ads-consent": "granted"})["appendedBefore"])

    def test_stored_choice_is_applied_before_the_tag(self) -> None:
        calls = self.run_snippet(stored={"ads-consent": "denied"})["calls"]
        update = calls.index(["consent", "update", {"ad_storage": "denied", "ad_user_data": "denied"}])
        config = [c[:2] for c in calls].index(["config", "AW-000000000"])
        self.assertLess(update, config)


if __name__ == "__main__":
    unittest.main()
