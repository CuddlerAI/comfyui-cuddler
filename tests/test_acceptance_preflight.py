"""The acceptance preflight cannot turn a readiness check into a paid/upload request."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from cuddler_plugin.cuddler.errors import CuddlerError
from cuddler_plugin.cuddler.transport import Request
from tests import fakes

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("acceptance_preflight", ROOT / "scripts" / "acceptance_preflight.py")
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


class PreflightTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "run"
        self.transport = fakes.FakeTransport()
        self.stdout = io.StringIO()

    def script_ready(self, *, balance=None):
        catalog = json.loads((ROOT / "data" / "catalog_snapshot.json").read_text())
        self.transport.add("GET", "/catalog", body=catalog)
        self.transport.add("GET", "/balance", body=balance or fakes.balance())
        self.transport.add("POST", "/videos/estimate", body=fakes.estimate())

    def run_preflight(self, *extra, environment=None, directory=None):
        argv = ["--output-dir", str(directory or self.directory), *extra]
        environment = {"CUDDLER_API_KEY": fakes.LIVE_KEY} if environment is None else environment
        with contextlib.redirect_stdout(self.stdout):
            result = preflight.main(argv, environ=environment, transport=self.transport)
        path = (directory or self.directory) / "preflight.json"
        return result, json.loads(path.read_text()) if path.exists() else None

    def test_success_writes_only_a_sanitized_private_zero_generation_receipt(self):
        self.script_ready()
        result, report = self.run_preflight()
        self.assertEqual((result, report["status"], report["paid_acceptance"]), (0, "passed", "not_started"))
        self.assertEqual(report["budget_approval"], "not_verified")
        self.assertEqual([(r.method, r.url) for r in self.transport.requests],
                         [("GET", fakes.API + "/catalog"), ("GET", fakes.API + "/balance"),
                          ("POST", fakes.API + "/videos/estimate")])
        self.assertNotIn("prompt", json.loads(self.transport.requests[-1].body))
        encoded = json.dumps(report) + self.stdout.getvalue()
        for sensitive in (fakes.LIVE_KEY, "Authorization", "available_usd", "daily_limit_usd", "expires_at"):
            self.assertNotIn(sensitive, encoded)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((self.directory / "preflight.json").stat().st_mode), 0o600)

    def test_missing_invalid_and_test_keys_stop_before_any_http(self):
        for name, value, expected in (("missing", None, "key_missing"),
                                      ("invalid", "bad-key", "key_invalid_format"),
                                      ("test", fakes.TEST_KEY, "test_key")):
            with self.subTest(name=name):
                result, report = self.run_preflight(environment={} if value is None else {"CUDDLER_API_KEY": value},
                                                    directory=Path(self.tmp.name) / name)
                self.assertEqual((result, report["error_code"]), (1, expected))
        self.assertEqual(self.transport.requests, [])

    def test_generation_upload_and_media_requests_are_refused_before_transport(self):
        guarded = preflight.PreflightTransport(self.transport)
        for method, url in (("POST", fakes.API + "/videos"), ("POST", fakes.API + "/uploads"),
                            ("PUT", fakes.PUT_URL), ("GET", fakes.OUTPUT_URL)):
            with self.subTest(method=method, url=url), self.assertRaises(CuddlerError) as caught:
                guarded.send(Request(method, url))
            self.assertEqual(caught.exception.code, "preflight_write_refused")
        with self.assertRaises(CuddlerError):
            guarded.stream(Request("GET", fakes.OUTPUT_URL), io.BytesIO())
        self.assertEqual(self.transport.requests, [])

    def test_redirects_are_blocked_without_exposing_the_location_or_body(self):
        self.transport.add("GET", "/catalog", status=302, body={"secret": fakes.LIVE_KEY},
                           headers={"Location": "https://elsewhere.example/?secret=" + fakes.LIVE_KEY})
        result, report = self.run_preflight()
        self.assertEqual((result, report["error_code"]), (1, "redirect_refused"))
        self.assertEqual(len(self.transport.requests), 1)
        self.assertNotIn(fakes.LIVE_KEY, json.dumps(report) + self.stdout.getvalue())
        self.assertNotIn("elsewhere.example", json.dumps(report) + self.stdout.getvalue())

    def test_transport_exception_text_is_never_written_or_printed(self):
        self.transport.fail("GET", "/catalog", RuntimeError("Bearer " + fakes.LIVE_KEY))
        result, report = self.run_preflight()
        self.assertEqual((result, report["error_code"]), (1, "preflight_failed"))
        self.assertNotIn(fakes.LIVE_KEY, json.dumps(report) + self.stdout.getvalue())

    def test_receipts_are_not_written_inside_the_checkout_or_overwritten(self):
        result, report = self.run_preflight(directory=ROOT / "private-acceptance-output")
        self.assertEqual((result, report), (1, None))
        self.directory.mkdir()
        original = b"existing evidence must stay intact"
        (self.directory / "preflight.json").write_bytes(original)
        with contextlib.redirect_stdout(self.stdout):
            result = preflight.main(["--output-dir", str(self.directory)], environ={}, transport=self.transport)
        self.assertEqual(result, 1)
        self.assertEqual((self.directory / "preflight.json").read_bytes(), original)
        self.assertEqual(self.transport.requests, [])

    def test_failed_cap_balance_or_daily_headroom_stays_zero_generation(self):
        for name, balance, extra, failed in (
                ("cap", fakes.balance(), ["--cap-usd", "0.01"], "cap_covers_hold"),
                ("balance", fakes.balance(available="0.000000"), [], "available_balance_covers_hold"),
                ("daily", fakes.balance() | {"spent_today_usd": "300.000000"}, [],
                 "account_daily_remaining_covers_hold")):
            with self.subTest(name=name):
                self.transport = fakes.FakeTransport()
                self.script_ready(balance=balance)
                result, report = self.run_preflight(*extra, directory=Path(self.tmp.name) / name)
                self.assertEqual((result, report["status"], report["checks"][failed]), (1, "blocked", False))
                self.assertTrue(all((request.method, request.url) in preflight.ALLOWED
                                    for request in self.transport.requests))

    def test_zero_daily_limit_means_unlimited_even_after_previous_spend(self):
        self.script_ready(balance=fakes.balance() | {"daily_limit_usd": "0.000000",
                                                     "spent_today_usd": "100.000000"})
        result, report = self.run_preflight()
        self.assertEqual((result, report["status"]), (0, "passed"))
        self.assertTrue(report["checks"]["account_daily_remaining_covers_hold"])

    def test_finite_daily_headroom_includes_every_inflight_hold(self):
        self.script_ready(balance=fakes.balance() | {"daily_limit_usd": "1.000000", "held_usd": "0.800000",
                                                     "spent_today_usd": "0.000000"})
        self.transport.routes[("POST", fakes.API + "/videos/estimate")] = [
            (200, fakes.estimate(hold="0.700000"), {})]
        result, report = self.run_preflight()
        self.assertEqual((result, report["status"]), (1, "blocked"))
        self.assertFalse(report["checks"]["account_daily_remaining_covers_hold"])
        self.assertTrue(report["checks"]["available_balance_covers_hold"])
        self.assertEqual(report["paid_requests_sent"], 0)

    def test_negative_estimates_cannot_pass_a_readiness_check(self):
        for name, quote in (("negative_usd", fakes.estimate(usd="-0.010000")),
                            ("negative_hold", fakes.estimate(hold="-0.010000"))):
            with self.subTest(name=name):
                self.transport = fakes.FakeTransport()
                self.script_ready()
                self.transport.routes[("POST", fakes.API + "/videos/estimate")] = [(200, quote, {})]
                result, report = self.run_preflight(directory=Path(self.tmp.name) / name)
                self.assertEqual((result, report["error_code"]), (1, "invalid_response"))
                self.assertTrue(all((request.method, request.url) in preflight.ALLOWED
                                    for request in self.transport.requests))

    def test_invalid_balance_envelopes_and_amounts_stop_before_estimation(self):
        missing_expiry = fakes.balance()
        missing_expiry.pop("expires_at")
        for name, balance in (
                ("currency", fakes.balance() | {"currency": "eur"}),
                ("expiry_type", fakes.balance() | {"expires_at": {"date": "later"}}),
                ("expiry_missing", missing_expiry),
                ("amount_type", fakes.balance() | {"available_usd": 12.34}),
                ("negative_limit", fakes.balance() | {"daily_limit_usd": "-1.000000"}),
                ("negative_spend", fakes.balance() | {"spent_today_usd": "-1.000000"})):
            with self.subTest(name=name):
                self.transport = fakes.FakeTransport()
                self.script_ready(balance=balance)
                result, report = self.run_preflight(directory=Path(self.tmp.name) / name)
                self.assertEqual((result, report["error_code"]), (1, "invalid_response"))
                self.assertEqual(self.transport.calls("POST", "/videos/estimate"), [])


if __name__ == "__main__":
    unittest.main()
