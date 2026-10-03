from __future__ import annotations

import unittest

from fakes import Harness, gaql_error

CAMPAIGN = "11111111111"
OTHER = "11111111112"


class SpendWatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness(self)
        self.g = self.h.google
        self.g.add_campaign(CAMPAIGN, "Scale test", budget_micros=40_000_000)
        # The billing counter lags behind real-time metrics.
        self.g.account_budgets = [{"id": "7", "status": "APPROVED", "amountServedMicros": "900000000",
                                   "approvedSpendingLimitMicros": "2000000000",
                                   "approvedStartDateTime": "2030-04-01 00:00:00"}]  # fmt: skip
        self.g.cost_micros = 950_000_000

    def budget(self) -> int:
        return self.g.budgets[self.g.campaigns[CAMPAIGN]["budget"]]["amountMicros"]

    def applied(self) -> list[dict]:
        return [m for m in self.g.mutations if not m["validate"]]

    def test_once_below_target_reports_both_counters(self) -> None:
        result = self.h.run(
            "google", "spend-watch", "--target", "1200", "--then-budget", f"{CAMPAIGN}=10", "--once", json_mode=True
        )
        self.assertEqual(0, result.code, result.err)
        data = result.json()
        self.assertFalse(data["target_reached"])
        self.assertEqual("950", data["spend"])
        self.assertEqual("900", data["billing"])
        self.assertEqual("2030-04-01", data["since"])
        query = next(q for q in self.g.queries if q.startswith("SELECT metrics.cost_micros FROM customer"))
        self.assertIn("BETWEEN '2030-04-01' AND '2030-05-03'", query)
        self.assertEqual(1, len([m for m in self.g.mutations if m["validate"]]), "actions are validated up front")
        self.assertEqual([], self.applied())

    def test_uses_the_larger_counter_and_dry_run_only_prints(self) -> None:
        result = self.h.run("google", "spend-watch", "--target", "925", "--then-budget", f"{CAMPAIGN}=10", "--once")
        self.assertEqual(0, result.code, result.err)
        self.assertIn("TARGET   reached", result.out)
        self.assertIn("DRY RUN  would set campaign 11111111111 daily budget to 10", result.out)
        self.assertEqual([], self.applied())
        self.assertEqual(40_000_000, self.budget())

    def test_billing_source_only(self) -> None:
        result = self.h.run("google", "spend-watch", "--target", "925", "--source", "billing", "--once", json_mode=True)
        self.assertEqual(0, result.code, result.err)
        self.assertFalse(result.json()["target_reached"])

    def test_applies_once_and_remembers(self) -> None:
        argv = [
            "google",
            "spend-watch",
            "--target",
            "925",
            "--then-budget",
            f"{CAMPAIGN}=10",
            "--then-pause",
            CAMPAIGN,
            "--apply",
        ]
        result = self.h.run(*argv)
        self.assertEqual(0, result.code, result.err)
        self.assertEqual(10_000_000, self.budget())
        self.assertEqual("PAUSED", self.g.campaigns[CAMPAIGN]["status"])
        phases = [e["phase"] for e in self.h.journal()]
        self.assertEqual(["intent", "applied", "verified"] * 2, phases)
        fired = list(self.h.state()["spend_watch"].values())[0]
        self.assertEqual("950", fired["spend"])

        before = len(self.g.mutations)
        result = self.h.run(*argv)
        self.assertEqual(0, result.code)
        self.assertIn("already fired", result.out)
        self.assertEqual(before, len(self.g.mutations))

        result = self.h.run(*argv, "--reset")
        self.assertEqual(0, result.code, result.err)
        self.assertIn("NO-OP", result.out)  # values are already in place: nothing is re-sent

    def test_loop_polls_until_the_target(self) -> None:
        polls = {"n": 0}

        def cost(query: str):  # type: ignore[no-untyped-def]
            polls["n"] += 1
            spent = 950_000_000 + polls["n"] * 30_000_000
            return [{"metrics": {"costMicros": str(spent)}}]

        self.g.on_query(r"SELECT metrics\.cost_micros FROM customer", cost)
        result = self.h.run("google", "spend-watch", "--target", "1040", "--then-budget", f"{CAMPAIGN}=10", "--apply")
        self.assertEqual(0, result.code, result.err)
        self.assertEqual(3, polls["n"])
        self.assertEqual([900, 900], self.h.sleeps)
        self.assertEqual(10_000_000, self.budget())

    def test_max_hours_stops_without_acting(self) -> None:
        result = self.h.run("google", "spend-watch", "--target", "5000", "--then-pause", CAMPAIGN, "--interval", "3600",
                            "--max-hours", "2", "--apply")  # fmt: skip
        self.assertEqual(0, result.code, result.err)
        self.assertIn("--max-hours reached", result.out)
        self.assertEqual("ENABLED", self.g.campaigns[CAMPAIGN]["status"])

    def test_poll_failures_are_tolerated(self) -> None:
        calls = {"n": 0}

        def flaky(query: str):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            if calls["n"] == 1:
                return gaql_error(400, "requestError", "BAD_QUERY", "temporary trouble")
            return [{"metrics": {"costMicros": "950000000"}}]

        self.g.on_query(r"SELECT metrics\.cost_micros FROM customer", flaky)
        result = self.h.run("google", "spend-watch", "--target", "925", "--since", "2030-04-01")
        self.assertEqual(0, result.code, result.err)
        self.assertIn("poll failed (1/10)", result.err)
        self.assertIn("TARGET   reached", result.out)
        self.assertEqual([900], self.h.sleeps)

    def test_a_temporary_account_budget_error_is_a_poll_failure(self) -> None:
        busy = gaql_error(503, "internalError", "TRANSIENT_ERROR", "busy")
        answers = iter([busy] * 3 + [None] * 10)  # the client tries three times, then the watch polls again

        def budgets(query: str):  # type: ignore[no-untyped-def]
            reply = next(answers)
            return reply if reply else [{"accountBudget": dict(self.g.account_budgets[0])}]

        self.g.on_query(r"FROM account_budget", budgets)
        result = self.h.run("google", "spend-watch", "--target", "925", "--interval", "60")
        self.assertEqual(0, result.code, result.err)
        self.assertIn("poll failed (1/10)", result.err)
        self.assertIn("TARGET   reached", result.out)

    def test_token_endpoint_outage_during_a_watch_is_retried(self) -> None:
        self.g.token_expires_in = 600  # the access token runs out between two polls
        polls = {"n": 0}

        def cost(query: str):  # type: ignore[no-untyped-def]
            polls["n"] += 1
            if polls["n"] == 1:  # Google's token endpoint goes down for the next refresh (three tries)
                self.g.token_failures = [(503, {}, {"error": "temporarily_unavailable"})] * 3
            return [{"metrics": {"costMicros": "950000000" if polls["n"] == 1 else "1100000000"}}]

        self.g.on_query(r"SELECT metrics\.cost_micros FROM customer", cost)
        result = self.h.run("google", "spend-watch", "--target", "1000", "--then-budget", f"{CAMPAIGN}=10", "--apply")
        self.assertEqual(0, result.code, result.err)
        self.assertIn("poll failed (1/10)", result.err)
        self.assertIn("OAuth token endpoint answered HTTP 503", result.err)
        self.assertEqual([900, 2, 4, 900], self.h.sleeps)  # two client retries, then the next poll
        self.assertNotIn("invalid_grant", result.err)
        self.assertEqual(10_000_000, self.budget())

    def test_since_counts_metrics_only(self) -> None:
        # The billing counter started months earlier: comparing it with --since would fire at once.
        self.g.account_budgets[0].update(amountServedMicros="5000000000", approvedStartDateTime="2030-01-01 00:00:00")
        self.g.cost_micros = 100_000_000
        result = self.h.run("google", "spend-watch", "--target", "1000", "--since", "2030-05-01",
                            "--then-pause", CAMPAIGN, "--once", "--apply", json_mode=True)  # fmt: skip
        self.assertEqual(0, result.code, result.err)
        data = result.json()
        self.assertFalse(data["target_reached"])
        self.assertEqual("100", data["spend"])
        self.assertIsNone(data["billing"])
        self.assertIn("billing counter ignored", result.err)
        self.assertFalse([q for q in self.g.queries if "account_budget" in q])
        query = next(q for q in self.g.queries if q.startswith("SELECT metrics.cost_micros FROM customer"))
        self.assertIn("BETWEEN '2030-05-01' AND '2030-05-03'", query)
        self.assertEqual("ENABLED", self.g.campaigns[CAMPAIGN]["status"])

    def test_since_does_not_go_with_the_billing_source_or_the_future(self) -> None:
        result = self.h.run("google", "spend-watch", "--target", "10", "--source", "billing", "--since", "2030-05-01")
        self.assertEqual(2, result.code)
        self.assertIn("--since applies to the metrics counter", result.err)
        self.assertEqual(2, self.h.run("google", "spend-watch", "--target", "10", "--since", "2030-06-01").code)

    def test_without_an_account_budget_since_is_required(self) -> None:
        self.g.account_budgets = []
        self.g.cost_micros = 20_000_000_000  # all-time spend far above the target
        result = self.h.run("google", "spend-watch", "--target", "1500", "--then-pause", CAMPAIGN, "--apply")
        self.assertEqual(2, result.code)
        self.assertIn("no start date to count spend from", result.err)
        self.assertIn("--since", result.err)
        self.assertEqual("ENABLED", self.g.campaigns[CAMPAIGN]["status"])
        self.assertFalse([m for m in self.g.mutations if not m["validate"]])
        for source in ("metrics", "max"):  # the metrics source too: no silent all-time count
            self.assertEqual(2, self.h.run("google", "spend-watch", "--target", "1500", "--source", source).code)
        self.g.cost_micros = 100_000_000
        result = self.h.run("google", "spend-watch", "--target", "1500", "--since", "2030-05-01", "--once")
        self.assertEqual(0, result.code, result.err)

    def test_a_refused_account_budget_query_needs_since(self) -> None:
        self.g.on_query(
            r"FROM account_budget", gaql_error(400, "authorizationError", "ACTION_NOT_PERMITTED", "no access")
        )
        result = self.h.run("google", "spend-watch", "--target", "925", "--once")
        self.assertEqual(2, result.code)
        self.assertIn("ACTION_NOT_PERMITTED", result.err)
        self.assertIn("--since", result.err)

    def test_metrics_source_counts_from_the_budget_start(self) -> None:
        result = self.h.run(
            "google", "spend-watch", "--target", "2000", "--source", "metrics", "--once", json_mode=True
        )
        self.assertEqual(0, result.code, result.err)
        self.assertEqual("2030-04-01", result.json()["since"])

    def test_one_failing_action_does_not_stop_the_others(self) -> None:
        self.g.add_campaign(OTHER, "Second", budget_micros=40_000_000)

        def cost(query: str):  # type: ignore[no-untyped-def]
            del self.g.campaigns[CAMPAIGN]  # removed from the account between the preflight and the target
            return [{"metrics": {"costMicros": "950000000"}}]

        self.g.on_query(r"SELECT metrics\.cost_micros FROM customer", cost)
        result = self.h.run("google", "spend-watch", "--target", "925", "--then-pause", CAMPAIGN,
                            "--then-pause", OTHER, "--apply", json_mode=True)  # fmt: skip
        self.assertEqual(3, result.code)
        self.assertIn(f"pause campaign {CAMPAIGN}: campaign {CAMPAIGN} was not found", result.err)
        self.assertEqual("PAUSED", self.g.campaigns[OTHER]["status"])
        self.assertEqual([3, 0], [a["exit_code"] for a in result.json()["actions"]])
        self.assertFalse(self.h.state().get("spend_watch"), "a watch with a failed action is not marked as fired")

    def test_pauses_run_before_budget_changes(self) -> None:
        self.g.add_campaign(OTHER, "Second", budget_micros=40_000_000)
        result = self.h.run("google", "spend-watch", "--target", "925", "--then-budget", f"{OTHER}=10",
                            "--then-pause", CAMPAIGN, "--apply")  # fmt: skip
        self.assertEqual(0, result.code, result.err)
        applied = [e["action"] for e in self.h.journal() if e["phase"] == "applied"]
        self.assertEqual(["campaign.pause", "budget.set"], applied)

    def test_a_removed_campaign_counts_as_paused(self) -> None:
        def cost(query: str):  # type: ignore[no-untyped-def]
            self.g.campaigns[CAMPAIGN]["status"] = "REMOVED"
            return [{"metrics": {"costMicros": "950000000"}}]

        self.g.on_query(r"SELECT metrics\.cost_micros FROM customer", cost)
        result = self.h.run("google", "spend-watch", "--target", "925", "--then-pause", CAMPAIGN, "--apply")
        self.assertEqual(0, result.code, result.err)
        self.assertIn("REMOVED and does not serve", result.out)
        self.assertTrue(self.h.state()["spend_watch"])

    def test_once_fails_fast_for_cron(self) -> None:
        self.g.on_query(
            r"SELECT metrics\.cost_micros FROM customer", gaql_error(400, "requestError", "BAD_QUERY", "nope")
        )
        result = self.h.run("google", "spend-watch", "--target", "925", "--once")
        self.assertEqual(1, result.code)
        self.assertEqual([], self.h.sleeps)

    def test_guards_are_checked_before_watching(self) -> None:
        result = self.h.run("google", "spend-watch", "--target", "5000", "--then-budget", f"{CAMPAIGN}=500", "--apply")
        self.assertEqual(3, result.code)
        self.assertIn("above the configured ceiling", result.err)
        self.assertEqual([], self.g.mutations)
        self.assertEqual([], self.h.sleeps)

    def test_bad_arguments(self) -> None:
        self.assertEqual(2, self.h.run("google", "spend-watch", "--target", "0", "--once").code)
        self.assertEqual(2, self.h.run("google", "spend-watch", "--target", "10", "--interval", "5").code)
        self.assertEqual(
            2, self.h.run("google", "spend-watch", "--target", "10", "--then-budget", "123", "--once").code
        )
        for hours in ("0", "-1"):
            result = self.h.run("google", "spend-watch", "--target", "10", "--max-hours", hours)
            self.assertEqual(2, result.code, hours)
            self.assertIn("--max-hours must be above zero", result.err)


if __name__ == "__main__":
    unittest.main()
