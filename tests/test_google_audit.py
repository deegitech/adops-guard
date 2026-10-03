from __future__ import annotations

import unittest
from datetime import date

from adops_guard.config import AuditSettings
from adops_guard.google.audit import AuditData, Breakdown, Placement, Totals, analyze, channel_label, verdict
from fakes import Harness, gaql_error


def totals(impressions: int, clicks: int, cost: float) -> Totals:
    return Totals(impressions, clicks, int(cost * 1_000_000))


def metrics(impressions: int, clicks: int, cost: float) -> dict:
    return {"impressions": str(impressions), "clicks": str(clicks), "costMicros": str(int(cost * 1_000_000))}


def data_with(**parts) -> AuditData:  # type: ignore[no-untyped-def]
    data = AuditData(date(2030, 4, 20), date(2030, 5, 3), None, "USD", "UTC")
    for key, value in parts.items():
        setattr(data, key, value)
    return data


class LabelTests(unittest.TestCase):
    def test_channel_labels(self) -> None:
        self.assertEqual("youtube in-stream", channel_label("YOUTUBE", "INSTREAM_SKIPPABLE"))
        self.assertEqual("youtube in-stream", channel_label("YOUTUBE", "BUMPER"))
        self.assertEqual("youtube in-feed", channel_label("YOUTUBE", "INFEED"))
        self.assertEqual("youtube shorts", channel_label(None, "SHORTS"))
        self.assertEqual("discover", channel_label("DISCOVER", "UNSEGMENTED"))
        self.assertEqual("youtube", channel_label("YOUTUBE", None))
        self.assertEqual("unknown", channel_label(None, None))


class AnalysisTests(unittest.TestCase):
    settings = AuditSettings()

    def signal(self, data: AuditData, key: str):  # type: ignore[no-untyped-def]
        return next(s for s in analyze(data, self.settings) if s.key == key)

    def test_instream_fires_when_ctr_is_far_above_other_formats(self) -> None:
        channels = Breakdown(
            rows={
                "youtube in-stream": totals(50_000, 2_000, 400),
                "youtube shorts": totals(40_000, 300, 150),
                "youtube in-feed": totals(10_000, 100, 40),
            },  # fmt: skip
            source="segments.ad_format_type",
        )
        signal = self.signal(data_with(channels=channels, channel_mode="format"), "instream-ctr")
        self.assertEqual("fired", signal.status)
        self.assertIn("5.0x", signal.detail)
        self.assertIn("83.3% of all clicks", signal.detail)
        self.assertIn("--youtube-in-stream off", signal.advice)

    def test_instream_clear_and_not_enough_data(self) -> None:
        even = Breakdown(
            rows={"youtube in-stream": totals(50_000, 600, 300), "youtube shorts": totals(50_000, 500, 300)}, source="x"
        )
        self.assertEqual("clear", self.signal(data_with(channels=even, channel_mode="format"), "instream-ctr").status)
        small = Breakdown(
            rows={"youtube in-stream": totals(500, 10, 5), "youtube shorts": totals(500, 1, 1)}, source="x"
        )
        self.assertEqual("n/a", self.signal(data_with(channels=small, channel_mode="format"), "instream-ctr").status)
        network = Breakdown(rows={"youtube": totals(90_000, 3_000, 600)}, source="segments.ad_network_type")
        self.assertEqual("n/a", self.signal(data_with(channels=network, channel_mode="network"), "instream-ctr").status)

    def test_kids_and_tv_drama_placements(self) -> None:
        placements = [
            Placement("channel", "Nursery Rhymes TV", "UC1", "YOUTUBE_CHANNEL", "", totals(9000, 300, 60), ["kids"]),
            Placement("channel", "Gaming News", "UC2", "YOUTUBE_CHANNEL", "", totals(9000, 100, 40), []),
            Placement(
                "video",
                "Love Story Episode 12 Full Episode",
                "v1",
                "YOUTUBE_VIDEO",
                "",
                totals(4000, 200, 30),
                ["tv-drama"],
            ),
        ]
        signal = self.signal(data_with(placements=placements, placements_available=True), "kids-tv-placements")
        self.assertEqual("fired", signal.status)
        self.assertIn("Nursery Rhymes TV", signal.detail)

    def test_keyword_matching_uses_word_boundaries(self) -> None:
        from adops_guard.google.audit import DRAMA_WORDS, KIDS_WORDS, _matcher

        kids, drama = _matcher(KIDS_WORDS), _matcher(DRAMA_WORDS)
        self.assertTrue(kids.search("Fun Kids Songs"))
        self.assertFalse(kids.search("Kidney health"))
        self.assertFalse(kids.search("Toyota reviews"))
        self.assertTrue(drama.search("Ep. 12 | My Drama"))
        self.assertFalse(drama.search("Epic gameplay"))

    def test_night_share(self) -> None:
        rows = {str(h): totals(1000, 5, 1) for h in range(24)}
        for h in range(0, 6):
            rows[str(h)] = totals(1000, 60, 6)
        signal = self.signal(data_with(hours=Breakdown(rows=rows, source="segments.hour")), "night-share")
        self.assertEqual("fired", signal.status)
        rows = {str(h): totals(1000, 10, 2) for h in range(24)}
        self.assertEqual("clear", self.signal(data_with(hours=Breakdown(rows=rows, source="x")), "night-share").status)

    def test_night_window_can_wrap_midnight(self) -> None:
        settings = AuditSettings(night_hours=(22, 4))
        rows = {str(h): totals(1000, 2, 1) for h in range(24)}
        for h in (22, 23, 0, 1, 2, 3):
            rows[str(h)] = totals(1000, 50, 5)
        data = data_with(hours=Breakdown(rows=rows, source="segments.hour"))
        signal = next(s for s in analyze(data, settings) if s.key == "night-share")
        self.assertEqual("fired", signal.status)

    def test_ctr_up_cpc_down(self) -> None:
        days = {}
        for i in range(9):
            clicks = 50 + i * 40
            days[f"2030-04-{20 + i:02d}"] = totals(10_000, clicks, clicks * (1.0 - i * 0.08))
        signal = self.signal(data_with(days=Breakdown(rows=days, source="segments.date")), "ctr-up-cpc-down")
        self.assertEqual("fired", signal.status)
        flat = {f"2030-04-{20 + i:02d}": totals(10_000, 100, 50) for i in range(9)}
        self.assertEqual(
            "clear", self.signal(data_with(days=Breakdown(rows=flat, source="x")), "ctr-up-cpc-down").status
        )
        short = {f"2030-04-{20 + i:02d}": totals(10_000, 100, 50) for i in range(3)}
        self.assertEqual(
            "n/a", self.signal(data_with(days=Breakdown(rows=short, source="x")), "ctr-up-cpc-down").status
        )

    def test_verdict_wording(self) -> None:
        empty = analyze(data_with(), self.settings)
        self.assertEqual("Not enough data for any signal.", verdict(empty))


class AuditCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness(self)
        g = self.h.google
        g.on_query(r"SELECT segments\.ad_network_type, segments\.ad_format_type", [
            {"segments": {"adNetworkType": "YOUTUBE", "adFormatType": "INSTREAM_SKIPPABLE"}, "metrics": metrics(50_000, 2_000, 400)},
            {"segments": {"adNetworkType": "YOUTUBE", "adFormatType": "SHORTS"}, "metrics": metrics(40_000, 300, 150)},
            {"segments": {"adNetworkType": "DISCOVER", "adFormatType": "UNSEGMENTED"}, "metrics": metrics(5_000, 50, 25)},
        ])  # fmt: skip
        g.on_query(r"FROM group_placement_view", [
            {"groupPlacementView": {"placementType": "YOUTUBE_CHANNEL", "displayName": "Kids Cartoons", "placement": "UCa"},
             "metrics": metrics(20_000, 900, 100)},
            {"groupPlacementView": {"placementType": "YOUTUBE_CHANNEL", "displayName": "Kids Cartoons", "placement": "UCa"},
             "metrics": metrics(1_000, 100, 10)},
            {"groupPlacementView": {"placementType": "YOUTUBE_CHANNEL", "displayName": "Speedrun Clips", "placement": "UCb"},
             "metrics": metrics(20_000, 400, 100)},
        ])  # fmt: skip
        g.on_query(
            r"FROM detail_placement_view",
            gaql_error(400, "queryError", "PROHIBITED_RESOURCE_TYPE_IN_FROM_CLAUSE", "no"),
        )
        g.on_query(r"SELECT segments\.hour", [{"segments": {"hour": h}, "metrics": metrics(1000, 20, 4)} for h in range(1, 24)]
                   + [{"segments": {}, "metrics": metrics(1000, 20, 4)}])  # fmt: skip
        g.on_query(r"FROM age_range_view", [
            {"adGroupCriterion": {"ageRange": {"type": "AGE_RANGE_18_24"}}, "metrics": metrics(9000, 300, 60)},
            {"adGroupCriterion": {"ageRange": {"type": "AGE_RANGE_UNDETERMINED"}}, "metrics": metrics(9000, 700, 90)},
        ])  # fmt: skip
        g.on_query(r"SELECT segments\.date", [
            {"segments": {"date": f"2030-04-{20 + i:02d}"}, "metrics": metrics(10_000, 50 + i * 40, (50 + i * 40) * (1 - i * 0.08))}
            for i in range(9)
        ])  # fmt: skip

    def test_audit_report_and_signals(self) -> None:
        result = self.h.run("google", "audit", "--campaign", "11111111111")
        self.assertEqual(0, result.code, result.err)
        self.assertIn("Heuristic", result.out)
        self.assertIn("youtube in-stream", result.out)
        self.assertIn("[FIRED] In-stream CTR far above Shorts / in-feed", result.out)
        self.assertIn("[FIRED] Clicks from TV-drama or kids channels", result.out)
        self.assertIn("unavailable: detail_placement_view", result.out)
        self.assertIn("Verdict:", result.out)
        query = next(q for q in self.h.google.queries if "ad_format_type" in q)
        self.assertIn("campaign.id = 11111111111", query)
        self.assertIn("BETWEEN '2030-04-20' AND '2030-05-03'", query)

    def test_json_and_fail_on_signal(self) -> None:
        result = self.h.run("google", "audit", "--fail-on-signal", json_mode=True)
        self.assertEqual(3, result.code)
        data = result.json()
        self.assertTrue(data["heuristic"])
        fired = {s["key"] for s in data["signals"] if s["status"] == "fired"}
        self.assertIn("instream-ctr", fired)
        self.assertIn("ctr-up-cpc-down", fired)
        self.assertEqual(1000, data["placements"][0]["clicks"])  # the two rows for one channel were merged
        self.assertEqual(["kids"], data["placements"][0]["flags"])

    def test_falls_back_to_network_type_when_format_is_unavailable(self) -> None:
        g = self.h.google
        g.custom = [c for c in g.custom if "ad_format_type" not in c[0].pattern]
        g.on_query(
            r"segments\.ad_format_type", gaql_error(400, "queryError", "UNRECOGNIZED_FIELD", "Unrecognized field")
        )
        g.on_query(r"SELECT segments\.ad_network_type,", [
            {"segments": {"adNetworkType": "YOUTUBE"}, "metrics": metrics(90_000, 2_700, 650)},
        ])  # fmt: skip
        result = self.h.run("google", "audit", json_mode=True)
        self.assertEqual(0, result.code, result.err)
        data = result.json()
        self.assertEqual("segments.ad_network_type", data["channels"]["source"])
        self.assertEqual(2, len(data["channels"]["errors"]))
        instream = next(s for s in data["signals"] if s["key"] == "instream-ctr")
        self.assertEqual("n/a", instream["status"])


if __name__ == "__main__":
    unittest.main()
