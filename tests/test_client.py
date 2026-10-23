import io
import json
import unittest
from unittest import mock

from cuddler_plugin.cuddler.client import PlatformClient
from cuddler_plugin.cuddler.errors import CuddlerError
from cuddler_plugin.cuddler.money import display, from_micros, to_micros
from tests import fakes

JOB_PATHS = {"video": "videos", "image": "images", "speech": "speech", "music": "music"}


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.transport = fakes.FakeTransport()
        self.client = PlatformClient(self.transport, fakes.LIVE_KEY)

    def error_for(self, call, method, path, status, body, headers=None):
        """Script one response, run `call(client)` against it and return the CuddlerError it must raise."""
        transport = fakes.FakeTransport().add(method, path, status=status, body=body, headers=headers)
        with self.assertRaises(CuddlerError) as caught:
            call(PlatformClient(transport, fakes.LIVE_KEY))
        return caught.exception

    def test_catalog_needs_no_key(self):
        self.transport.add("GET", "/catalog", body={"object": "catalog", "models": [], "voices": {}})
        PlatformClient(self.transport).catalog()
        request = self.transport.requests[-1]
        self.assertNotIn("Authorization", request.headers)

    def test_sends_the_key_as_a_bearer_token(self):
        self.transport.add("GET", "/balance", body=fakes.balance())
        self.assertEqual(self.client.balance()["available_usd"], "12.340000")
        self.assertEqual(self.transport.requests[-1].headers["Authorization"], "Bearer " + fakes.LIVE_KEY)

    def test_calls_needing_a_key_fail_without_one(self):
        with self.assertRaises(CuddlerError) as caught:
            PlatformClient(self.transport).balance()
        self.assertEqual(caught.exception.code, "key_missing")
        self.assertEqual(self.transport.requests, [])

    def test_api_calls_refuse_untrusted_origins_before_sending_anything(self):
        origins = (
            "https://other.example", "https://api.cuddler.ai.other.example",
            "https://other-api.cuddler.ai", "https://acct.r2.cloudflarestorage.com",
            "http://api.cuddler.ai", "https://api.cuddler.ai:8443",
            "https://api.cuddler.ai@other.example", "https://user@api.cuddler.ai",
            "https://api.cuddler.ai/base", "https://api.cuddler.ai/../",
            "https://api.cuddler.ai?redirect=other", "https://api.cuddler.ai#fragment",
            "https://api.cuddler.ai?", "https://api.cuddler.ai#",
            "https://api.cuddler.ai\t.other.example", "https://api.cuddler.ai.",
            "https://127.0.0.1", "https://localhost", "file:///tmp/platform",
        )
        for origin in origins:
            client = PlatformClient(self.transport, fakes.LIVE_KEY, origin=origin)
            for operation in (client.catalog, client.balance,
                              lambda: client.estimate("image", {"model": "seedream-5"})):
                with self.subTest(origin=origin, operation=operation):
                    with self.assertRaises(CuddlerError) as caught:
                        operation()
                    self.assertEqual(caught.exception.code, "unsafe_url")
                    self.assertEqual(self.transport.requests, [])
                    self.assertNotIn(fakes.LIVE_KEY, str(caught.exception))

    def test_changing_an_origin_cannot_submit_a_job_elsewhere(self):
        self.client.origin = "https://other.example"
        with self.assertRaises(CuddlerError) as caught:
            self.client.create("video", {"model": "seedance-2.5"}, "job-0123456789abcdef")
        self.assertEqual(caught.exception.code, "unsafe_url")
        self.assertEqual(self.transport.requests, [])

    def test_valid_api_origins_keep_the_request_and_recovery_identity(self):
        for origin in ("https://api.cuddler.ai", "https://api.cuddler.ai/",
                       "https://api.cuddler.ai:443", "https://API.CUDDLER.AI"):
            with self.subTest(origin=origin):
                client = PlatformClient(self.transport, fakes.LIVE_KEY, origin=origin)
                identity = client.recovery_identity()
                url = origin.rstrip("/") + "/v1/balance"
                self.transport.add("GET", url, body=fakes.balance())
                self.assertEqual(client.balance()["available_usd"], "12.340000")
                request = self.transport.requests[-1]
                self.assertEqual((request.method, request.url, request.body), ("GET", url, None))
                self.assertEqual(request.headers, {"Accept": "application/json",
                                                  "Authorization": "Bearer " + fakes.LIVE_KEY})
                self.assertEqual(client.recovery_identity(), identity)

    def test_a_call_sends_to_the_origin_it_checked_even_if_the_client_changes(self):
        encode = json.dumps

        def change_origin(*args, **kwargs):
            self.client.origin = "https://other.example"
            return encode(*args, **kwargs)

        self.transport.add("POST", "/videos", status=202, body=fakes.video_job())
        with mock.patch("cuddler_plugin.cuddler.client.json.dumps", side_effect=change_origin):
            self.client.create("video", {"model": "seedance-2.5"}, "job-0123456789abcdef")
        self.assertEqual(self.transport.requests[-1].url, fakes.API + "/videos")
        with self.assertRaises(CuddlerError) as caught:
            self.client.balance()
        self.assertEqual(caught.exception.code, "unsafe_url")
        self.assertEqual(len(self.transport.requests), 1)

    def test_recovery_identity_binds_the_credential_and_origin_without_a_request(self):
        identity = self.client.recovery_identity()
        self.assertRegex(identity, r"^[0-9a-f]{64}$")
        self.assertEqual(identity, PlatformClient(self.transport, fakes.LIVE_KEY).recovery_identity())
        self.assertNotEqual(identity, PlatformClient(self.transport, fakes.TEST_KEY).recovery_identity())
        self.assertNotEqual(identity, PlatformClient(self.transport, fakes.LIVE_KEY,
                                                    origin="https://other.example").recovery_identity())
        self.assertNotIn(fakes.LIVE_KEY, identity)
        with self.assertRaises(CuddlerError) as caught:
            PlatformClient(self.transport).recovery_identity()
        self.assertEqual(caught.exception.code, "key_missing")
        self.assertEqual(self.transport.requests, [])

    def test_error_envelopes_become_cuddler_errors(self):
        self.transport.add("POST", "/videos/estimate", status=402, body=fakes.error("insufficient_balance", 402),
                           headers={"Retry-After": "30"})
        with self.assertRaises(CuddlerError) as caught:
            self.client.estimate("video", {"model": "seedance-2.5", "duration": 4})
        error = caught.exception
        self.assertEqual((error.code, error.status, error.request_id, error.retry_after),
                         ("insufficient_balance", 402, "req_123", 30))

    def test_unknown_errors_are_classified_by_status(self):
        self.transport.add("GET", "/balance", status=503, body=b"<html>down</html>", headers={"X-Request-Id": "req_9"})
        with self.assertRaises(CuddlerError) as caught:
            self.client.balance()
        self.assertEqual((caught.exception.code, caught.exception.request_id), ("platform_unavailable", "req_9"))

    def test_estimate_checks_the_object_for_its_kind(self):
        self.transport.add("POST", "/images/estimate", body=fakes.estimate("video"))
        with self.assertRaises(CuddlerError) as caught:
            self.client.estimate("image", {"model": "seedream-5"})
        self.assertEqual(caught.exception.code, "invalid_response")

    def test_create_sends_the_idempotency_key_and_reports_replays(self):
        self.transport.add("POST", "/videos", status=200, body=fakes.video_job(),
                           headers={"Idempotent-Replayed": "true"})
        job, replayed = self.client.create("video", {"model": "seedance-2.5"}, "job-0123456789abcdef")
        self.assertEqual((job["id"], replayed), (fakes.VIDEO_ID, True))
        request = self.transport.requests[-1]
        self.assertEqual(request.headers["Idempotency-Key"], "job-0123456789abcdef")
        self.assertEqual(json.loads(request.body), {"model": "seedance-2.5"})

    def test_job_responses_are_validated(self):
        bad = fakes.video_job(job_id="vid_short")
        self.transport.add("GET", "/videos/" + fakes.VIDEO_ID, body=bad)
        with self.assertRaises(CuddlerError):
            self.client.get("video", fakes.VIDEO_ID)
        self.transport.routes.clear()
        self.transport.add("GET", "/videos/" + fakes.VIDEO_ID, body=fakes.video_job("succeeded", output=None))
        with self.assertRaises(CuddlerError):
            self.client.get("video", fakes.VIDEO_ID)

    def test_refuses_ids_of_another_kind(self):
        with self.assertRaises(CuddlerError):
            self.client.get("image", fakes.VIDEO_ID)
        self.assertEqual(self.transport.requests, [])

    def test_uploads_and_media_transfers(self):
        self.transport.add("POST", "/uploads", status=201, body=fakes.upload())
        self.transport.add("PUT", fakes.PUT_URL, status=200, body=b"")
        created = self.client.create_upload({"kind": "image", "content_type": "image/png", "bytes": 4},
                                            "upl-0123456789abcdef")
        self.client.put_media(created["upload_url"], created["upload_headers"], b"\x89PNG")
        put = self.transport.calls("PUT", fakes.PUT_URL)[0]
        self.assertEqual((put.headers, put.body), ({"Content-Type": "image/png"}, b"\x89PNG"))

    def test_media_links_must_be_platform_storage(self):
        with self.assertRaises(CuddlerError) as caught:
            self.client.put_media("https://example.com/upload", {}, b"x")
        self.assertEqual(caught.exception.code, "media_host_not_allowed")

    def test_download_streams_into_a_sink(self):
        self.transport.add("GET", fakes.OUTPUT_URL, body=b"video-bytes")
        sink = io.BytesIO()
        self.client.download(fakes.OUTPUT_URL, sink, limit=1024)
        self.assertEqual(sink.getvalue(), b"video-bytes")
        self.assertNotIn("Authorization", self.transport.requests[-1].headers)

    # Review additions: what the Platform's OpenAPI allows, and what a garbled answer must not do.

    def test_retry_after_is_plain_seconds_or_nothing(self):
        cases = (("30", 30), ("0", 0), ("999999", 86_400), ("²", None), ("٣", None),
                 ("9" * 5000, None), ("Wed, 21 Oct 2026 07:28:00 GMT", None), ("-5", None), ("", None))
        for text, expected in cases:
            with self.subTest(retry_after=ascii(text[:12])):
                error = self.error_for(lambda c: c.balance(), "GET", "/balance", 429,
                                       fakes.error("rate_limited", 429), {"Retry-After": text})
                self.assertEqual((error.code, error.retry_after), ("rate_limited", expected))

    def test_errors_without_a_platform_body_follow_the_status(self):
        page = b"<html>edge</html>"
        for status, code in ((401, "unauthorized"), (429, "rate_limited"), (403, "invalid_response"),
                             (404, "invalid_response"), (413, "invalid_response"), (500, "platform_unavailable"),
                             (502, "platform_unavailable"), (524, "platform_unavailable")):
            with self.subTest(status=status):
                error = self.error_for(lambda c: c.balance(), "GET", "/balance", status, page,
                                       {"Retry-After": "7"})
                self.assertEqual((error.code, error.status, error.retry_after), (code, status, 7))
        unknown = {"code": "brand_new_code", "message": "x", "request_id": "req_5"}  # a code from a newer Platform
        self.assertEqual(self.error_for(lambda c: c.balance(), "GET", "/balance", 429, unknown).code, "rate_limited")
        self.assertEqual(self.error_for(lambda c: c.balance(), "GET", "/balance", 400, unknown).code,
                         "invalid_response")
        known = fakes.error("model_not_enabled", 403)  # a code this plugin knows always wins over the status
        self.assertEqual(self.error_for(lambda c: c.balance(), "GET", "/balance", 403, known).code,
                         "model_not_enabled")

    def test_garbled_responses_become_cuddler_errors(self):
        job = fakes.video_job()
        job["status"] = ["queued"]
        upload = fakes.upload()
        upload["status"] = {}
        video_path, upload_path = "/videos/" + fakes.VIDEO_ID, "/uploads/" + fakes.UPLOAD_ID
        cases = (
            ("error code is a list", "GET", "/balance", 500, {"code": ["x"], "request_id": "r"},
             lambda c: c.balance(), "platform_unavailable"),
            ("error code is a dict", "GET", "/balance", 400, {"code": {}}, lambda c: c.balance(),
             "invalid_response"),
            ("deeply nested success", "GET", "/balance", 200, b"[" * 100_000, lambda c: c.balance(),
             "invalid_response"),
            ("deeply nested error", "GET", "/balance", 502, b"[" * 100_000, lambda c: c.balance(),
             "platform_unavailable"),
            ("not an object", "GET", "/balance", 200, b"[]", lambda c: c.balance(), "invalid_response"),
            ("null", "GET", "/balance", 200, b"null", lambda c: c.balance(), "invalid_response"),
            ("job status is a list", "GET", video_path, 200, job,
             lambda c: c.get("video", fakes.VIDEO_ID), "invalid_response"),
            ("upload status is a dict", "GET", upload_path, 200, upload,
             lambda c: c.get_upload(fakes.UPLOAD_ID), "invalid_response"),
        )
        for label, method, path, status, body, call, code in cases:
            with self.subTest(label):
                self.assertEqual(self.error_for(call, method, path, status, body).code, code)

    def test_amounts_must_be_ones_money_py_can_read(self):
        for text in ("1234567890.000000", "١٢.000000", "012.000000", "1.5", "1.0000000", "1e3",
                     " 1.000000", "1.000000\n", "+1.000000", ".500000", ""):
            with self.subTest(amount=ascii(text)):
                body = fakes.balance()
                body["available_usd"] = text
                error = self.error_for(lambda c: c.balance(), "GET", "/balance", 200, body)
                self.assertEqual(error.code, "invalid_response")
        for text in ("0.000000", "-1.500000", "999999999.999999"):
            with self.subTest(amount=text):
                self.transport.routes.clear()
                self.transport.add("GET", "/balance", body=fakes.balance(text))
                shown = self.client.balance()["available_usd"]
                self.assertEqual(from_micros(to_micros(shown)), text)  # what the client accepts, money.py reads
                display(shown)

    def test_a_finished_job_whose_file_was_deleted_is_reported_as_expired(self):
        path = "/videos/" + fakes.VIDEO_ID
        error = self.error_for(lambda c: c.get("video", fakes.VIDEO_ID), "GET", path, 200,
                               fakes.video_job("succeeded", output=None))
        self.assertEqual(error.code, "output_expired")
        for output in ({}, {"url": 5}, {"url": None}, [], "https://x"):  # present but malformed
            with self.subTest(output=output):
                error = self.error_for(lambda c: c.get("video", fakes.VIDEO_ID), "GET", path, 200,
                                       fakes.video_job("succeeded", output=output))
                self.assertEqual(error.code, "invalid_response")

    def test_every_kind_of_job_round_trips(self):
        for kind, job_id in fakes.JOB_IDS.items():
            for status in ("queued", "running", "succeeded", "failed", "canceled"):
                with self.subTest(kind=kind, status=status):
                    self.transport.routes.clear()
                    self.transport.add("GET", f"/{JOB_PATHS[kind]}/{job_id}", body=fakes.job(kind, status))
                    self.assertEqual(self.client.get(kind, job_id)["status"], status)
            with self.subTest(kind=kind, call="estimate and create"):
                self.transport.routes.clear()
                self.transport.add("POST", f"/{JOB_PATHS[kind]}/estimate", body=fakes.estimate(kind))
                self.transport.add("POST", f"/{JOB_PATHS[kind]}", status=202, body=fakes.job(kind))
                self.assertEqual(self.client.estimate(kind, {"model": "m"})["usd"], "0.653877")
                created, replayed = self.client.create(kind, {"model": "m"}, "job-0123456789abcdef")
                self.assertEqual((created["id"], replayed), (job_id, False))

    def test_another_tasks_expiry_is_never_accepted_as_a_bound_response(self):
        for kind, job_id in fakes.JOB_IDS.items():
            body = fakes.job(kind, "succeeded", output=None)
            body["id"] = job_id[:-1] + "Z"
            for operation in ("get", "cancel"):
                if operation == "cancel" and kind == "image":
                    continue
                with self.subTest(kind=kind, operation=operation):
                    method = "GET" if operation == "get" else "POST"
                    path = f"/{JOB_PATHS[kind]}/{job_id}" + ("/cancel" if operation == "cancel" else "")
                    error = self.error_for(lambda c: getattr(c, operation)(kind, job_id), method, path, 200, body)
                    self.assertEqual(error.code, "invalid_response")

    def test_cancel_uses_the_cancel_route_and_images_have_none(self):
        self.transport.add("POST", f"/videos/{fakes.VIDEO_ID}/cancel", body=fakes.video_job("canceled"))
        self.transport.add("POST", f"/speech/{fakes.SPEECH_ID}/cancel", body=fakes.job("speech", "canceled"))
        self.assertEqual(self.client.cancel("video", fakes.VIDEO_ID)["status"], "canceled")
        self.assertEqual(self.client.cancel("speech", fakes.SPEECH_ID)["status"], "canceled")
        self.assertEqual([(r.method, r.body) for r in self.transport.requests], [("POST", None), ("POST", None)])
        with self.assertRaises(CuddlerError) as caught:
            self.client.cancel("image", fakes.IMAGE_ID)
        self.assertEqual(caught.exception.code, "not_cancelable")
        self.assertEqual(len(self.transport.requests), 2)  # the Platform has no such route, so nothing is sent

    def test_ids_cannot_change_the_request_path(self):
        base = "vid_" + "A" * 12
        for bad in (base + "/../balance", base + "?x=1", base + "#x", base + "\n", base + "%2e%2e", base + " ",
                    "vid_" + "١" * 20, "VID_" + "A" * 20, ""):
            with self.subTest(job_id=ascii(bad)):
                for call in (self.client.get, self.client.cancel):
                    with self.assertRaises(CuddlerError):
                        call("video", bad)
        for call in (self.client.get_upload, self.client.complete_upload):
            with self.assertRaises(CuddlerError):
                call("upl_" + "A" * 12 + "/../x")
        self.assertEqual(self.transport.requests, [])

    def test_idempotency_keys_are_checked_before_anything_is_sent(self):
        for bad in ("short", "a" * 129, "has a space", "ключ-ключ", "key-0123456789\n",
                    None, 12345678):
            with self.subTest(key=ascii(bad)):
                with self.assertRaises(CuddlerError) as caught:
                    self.client.create("video", {"model": "m"}, bad)
                self.assertEqual(caught.exception.code, "invalid_request")
                with self.assertRaises(CuddlerError) as caught:
                    self.client.create_upload({"kind": "image"}, bad)
                self.assertEqual(caught.exception.code, "invalid_request")
        self.assertEqual(self.transport.requests, [])
        self.transport.add("POST", "/uploads", status=201, body=fakes.upload())
        self.client.create_upload({"kind": "image"}, "upl-0123456789abcdef")
        self.assertEqual(self.transport.requests[-1].headers["Idempotency-Key"], "upl-0123456789abcdef")

    def test_a_first_create_is_not_a_replay(self):
        self.transport.add("POST", "/videos", status=202, body=fakes.video_job())
        job, replayed = self.client.create("video", {"model": "seedance-2.5"}, "job-0123456789abcdef")
        self.assertEqual((job["status"], replayed), ("queued", False))

    def test_media_transfers_never_carry_the_api_key(self):
        self.transport.add("PUT", fakes.PUT_URL, status=200, body=b"")
        self.transport.add("GET", fakes.OUTPUT_URL, body=b"x")
        self.client.put_media(fakes.PUT_URL, {"Content-Type": "image/png"}, b"x")
        self.client.download(fakes.OUTPUT_URL, io.BytesIO(), limit=10)
        self.assertEqual(len(self.transport.requests), 2)
        for request in self.transport.requests:
            names = {name.lower() for name in request.headers}
            self.assertFalse(names & {"authorization", "cookie", "proxy-authorization"})
            self.assertNotIn(fakes.LIVE_KEY, request.url)

    def test_media_uploads_reject_credentials_and_malformed_headers_before_sending(self):
        headers = (
            {"Authorization": "Bearer " + fakes.LIVE_KEY},
            {"aUtHoRiZaTiOn": "Bearer " + fakes.LIVE_KEY},
            {"Cookie": "session=private"}, {"PROXY-AUTHORIZATION": "Basic private"},
            {"Authorization ": "Bearer " + fakes.LIVE_KEY},
            {"X-Header\r\nAuthorization": "private"}, {"": "empty name"},
            {"X-Header": "value\r\nAuthorization: private"},
            {"X-Header": "value\nCookie: private"},
        )
        self.transport.add("PUT", fakes.PUT_URL, status=200, body=b"")
        for supplied in headers:
            with self.subTest(names=list(supplied)):
                with self.assertRaises(CuddlerError) as caught:
                    self.client.put_media(fakes.PUT_URL, supplied, b"data")
                self.assertEqual(caught.exception.code, "invalid_response")
                self.assertEqual(self.transport.requests, [])
                self.assertNotIn(fakes.LIVE_KEY, str(caught.exception))

    def test_media_uploads_preserve_legitimate_signed_headers_and_url(self):
        headers = {"content-type": "image/png", "content-length": "4",
                   "X-Amz-Checksum-Sha256": "checksum", "x-amz-meta-origin": "comfyui"}
        expected = dict(headers)
        self.transport.add("PUT", fakes.PUT_URL, status=200, body=b"")
        self.client.put_media(fakes.PUT_URL, headers, b"data")
        request = self.transport.requests[-1]
        self.assertEqual((request.method, request.url, request.headers, request.body),
                         ("PUT", fakes.PUT_URL, expected, b"data"))
        self.assertEqual(headers, expected)

    def test_media_uploads_send_the_header_snapshot_that_was_checked(self):
        class ChangingHeaders(dict):
            def items(self):
                original = list(super().items())
                self["Authorization"] = "Bearer " + fakes.LIVE_KEY
                return original

        self.transport.add("PUT", fakes.PUT_URL, status=200, body=b"")
        self.client.put_media(fakes.PUT_URL, ChangingHeaders({"Content-Type": "image/png"}), b"data")
        self.assertEqual(self.transport.requests[-1].headers, {"Content-Type": "image/png"})

    def test_the_key_never_shows_up_in_reprs_or_errors(self):
        self.transport.add("GET", "/balance", status=401, body=fakes.error("unauthorized", 401))
        with self.assertRaises(CuddlerError) as caught:
            self.client.balance()
        error = caught.exception
        shown = " ".join([repr(self.client), repr(self.transport.requests[-1]), repr(error), str(error),
                          repr(vars(error)), str(error.__cause__), str(error.__context__)])
        self.assertNotIn(fakes.LIVE_KEY, shown)
        self.assertNotIn("cud_sk_", shown)


if __name__ == "__main__":
    unittest.main()
