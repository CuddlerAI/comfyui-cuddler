import json
from pathlib import Path
import tempfile
import unittest

from cuddler_plugin.cuddler.client import PlatformClient
from cuddler_plugin.cuddler.errors import CuddlerError
from cuddler_plugin.cuddler.state import StateDB
from cuddler_plugin.cuddler.uploads import Uploader
from tests import fakes

PNG = b"\x89PNG-fake"
WAV = b"RIFF-fake"
UPLOAD = f"/uploads/{fakes.UPLOAD_ID}"
COMPLETE = UPLOAD + "/complete"


class UploaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.transport = fakes.FakeTransport()
        self.state = StateDB(Path(self.tmp.name))
        self.uploader = Uploader(PlatformClient(self.transport, fakes.LIVE_KEY), self.state, sleep=lambda s: None)

    def tearDown(self):
        self.tmp.cleanup()

    def script_happy_path(self):
        self.transport.add("POST", "/uploads", status=201, body=fakes.upload())
        self.transport.add("PUT", fakes.PUT_URL, body=b"")
        self.transport.add("POST", f"/uploads/{fakes.UPLOAD_ID}/complete", body=fakes.upload("ready"))

    def test_create_put_complete(self):
        self.script_happy_path()
        self.assertEqual(self.uploader.ensure(PNG, "image", "image/png", "frame.png"), fakes.UPLOAD_ID)
        create = self.transport.calls("POST", "/uploads")[0]
        self.assertTrue(create.headers["Idempotency-Key"].startswith("upl-"))
        self.assertEqual(self.transport.calls("PUT", fakes.PUT_URL)[0].body, PNG)

    def test_reuses_a_ready_upload_for_the_same_bytes(self):
        self.script_happy_path()
        self.uploader.ensure(PNG, "image", "image/png", "frame.png")
        self.transport.add("GET", f"/uploads/{fakes.UPLOAD_ID}", body=fakes.upload("ready"))
        self.assertEqual(self.uploader.ensure(PNG, "image", "image/png", "again.png"), fakes.UPLOAD_ID)
        self.assertEqual(len(self.transport.calls("POST", "/uploads")), 1)

    def test_a_known_upload_missing_under_the_current_key_requires_reconciliation(self):
        self.script_happy_path()
        self.uploader.ensure(PNG, "image", "image/png", "frame.png")
        self.transport.add("GET", f"/uploads/{fakes.UPLOAD_ID}", status=404, body=fakes.error("not_found", 404))
        with self.assertRaises(CuddlerError) as caught:
            self.uploader.ensure(PNG, "image", "image/png", "frame.png")
        self.assertEqual(caught.exception.code, "recovery_required")
        self.assertEqual(len(self.transport.calls("POST", "/uploads")), 1)

    def test_a_retry_after_a_crash_reuses_the_idempotency_key(self):
        self.transport.fail("POST", "/uploads", CuddlerError("network_error"))
        with self.assertRaises(CuddlerError):
            self.uploader.ensure(PNG, "image", "image/png", "frame.png")
        self.transport.routes.clear()
        self.script_happy_path()
        self.uploader.ensure(PNG, "image", "image/png", "frame.png")
        keys = {request.headers["Idempotency-Key"] for request in self.transport.calls("POST", "/uploads")}
        self.assertEqual(len(keys), 1)

    def test_waits_for_checks_to_finish(self):
        self.transport.add("POST", "/uploads", status=201, body=fakes.upload())
        self.transport.add("PUT", fakes.PUT_URL, body=b"")
        self.transport.add("POST", f"/uploads/{fakes.UPLOAD_ID}/complete", body=fakes.upload("pending", upload_url=None))
        self.transport.add("GET", f"/uploads/{fakes.UPLOAD_ID}", body=fakes.upload("pending", upload_url=None))
        self.transport.add("GET", f"/uploads/{fakes.UPLOAD_ID}", body=fakes.upload("ready"))
        self.assertEqual(self.uploader.ensure(PNG, "image", "image/png", "frame.png"), fakes.UPLOAD_ID)

    def test_rejections_explain_why(self):
        self.transport.add("POST", "/uploads", status=201, body=fakes.upload())
        self.transport.add("PUT", fakes.PUT_URL, body=b"")
        self.transport.add("POST", f"/uploads/{fakes.UPLOAD_ID}/complete",
                           body=fakes.upload("rejected", rejection_reason="content_policy"))
        with self.assertRaises(CuddlerError) as caught:
            self.uploader.ensure(PNG, "image", "image/png", "frame.png")
        self.assertEqual((caught.exception.code, caught.exception.detail["reason"]), ("upload_rejected", "content_policy"))

    def test_checks_type_and_size_before_any_request(self):
        for args, code in (((PNG, "image", "image/gif", "a.gif"), "media_type_not_allowed"),
                           ((b"", "image", "image/png", "a.png"), "media_too_large"),
                           ((PNG, "hologram", "image/png", "a.png"), "media_type_not_allowed")):
            with self.subTest(code=code), self.assertRaises(CuddlerError) as caught:
                self.uploader.ensure(*args)
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.transport.requests, [])

    def keys(self):
        return [request.headers["Idempotency-Key"] for request in self.transport.calls("POST", "/uploads")]

    def test_a_check_that_outlasts_the_wait_is_picked_up_by_the_next_run(self):
        still_checking = fakes.upload("pending", upload_url=None)
        self.transport.add("POST", "/uploads", status=201, body=fakes.upload())
        self.transport.add("POST", "/uploads", body=fakes.upload("ready"))  # the second run's replay
        self.transport.add("PUT", fakes.PUT_URL, body=b"")
        self.transport.add("POST", COMPLETE, body=still_checking)
        self.transport.add("GET", UPLOAD, body=still_checking)
        with self.assertRaises(CuddlerError) as caught:
            self.uploader.ensure(PNG, "image", "image/png", "frame.png")
        self.assertEqual(caught.exception.code, "upload_checking")
        self.assertEqual(self.uploader.ensure(PNG, "image", "image/png", "frame.png"), fakes.UPLOAD_ID)
        self.assertEqual(len(self.transport.calls("PUT", fakes.PUT_URL)), 1)  # the bytes went up once
        self.assertEqual(len(set(self.keys())), 1)                           # as one upload

    def test_bytes_the_platform_already_has_are_not_sent_again(self):
        self.transport.add("POST", "/uploads", status=201, body=fakes.upload())  # a replay answers with a fresh link
        self.transport.add("PUT", fakes.PUT_URL, body=b"")
        self.transport.add("POST", COMPLETE, status=503, body=fakes.error("unavailable", 503))
        self.transport.add("POST", COMPLETE, body=fakes.upload("ready"))
        with self.assertRaises(CuddlerError) as caught:
            self.uploader.ensure(PNG, "image", "image/png", "frame.png")
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertEqual(self.uploader.ensure(PNG, "image", "image/png", "frame.png"), fakes.UPLOAD_ID)
        self.assertEqual(len(self.transport.calls("PUT", fakes.PUT_URL)), 1)
        self.assertEqual(len(self.transport.calls("POST", COMPLETE)), 2)

    def test_a_resumed_upload_sends_the_bytes_when_the_platform_has_none(self):
        self.transport.add("POST", "/uploads", status=201, body=fakes.upload())
        self.transport.fail("PUT", fakes.PUT_URL, CuddlerError("network_error"))
        self.transport.add("PUT", fakes.PUT_URL, body=b"")
        self.transport.add("POST", COMPLETE, status=400, body=fakes.error("invalid_request", 400))  # nothing there yet
        self.transport.add("POST", COMPLETE, body=fakes.upload("ready"))
        with self.assertRaises(CuddlerError):
            self.uploader.ensure(PNG, "image", "image/png", "frame.png")
        self.assertEqual(self.uploader.ensure(PNG, "image", "image/png", "frame.png"), fakes.UPLOAD_ID)
        self.assertEqual(len(self.transport.calls("PUT", fakes.PUT_URL)), 2)  # the first never arrived
        self.assertEqual(len(set(self.keys())), 1)

    def test_an_upload_whose_link_ran_out_before_any_bytes_arrived_starts_over(self):
        self.transport.add("POST", "/uploads", status=201, body=fakes.upload())                  # run 1
        self.transport.add("POST", "/uploads", body=fakes.upload("pending", upload_url=None))    # run 2: the replay
        self.transport.add("POST", "/uploads", status=201, body=fakes.upload())                  # run 2: a new upload
        self.transport.fail("PUT", fakes.PUT_URL, CuddlerError("network_error"))
        self.transport.add("PUT", fakes.PUT_URL, body=b"")
        self.transport.add("POST", COMPLETE, status=400, body=fakes.error("invalid_request", 400))
        self.transport.add("POST", COMPLETE, body=fakes.upload("ready"))
        with self.assertRaises(CuddlerError):
            self.uploader.ensure(PNG, "image", "image/png", "frame.png")
        self.assertEqual(self.uploader.ensure(PNG, "image", "image/png", "frame.png"), fakes.UPLOAD_ID)
        first, replay, fresh = self.keys()
        self.assertEqual(first, replay)
        self.assertNotEqual(first, fresh)
        self.assertEqual(len(self.transport.calls("PUT", fakes.PUT_URL)), 2)

    def test_a_complete_that_times_out_is_followed_by_waiting_for_the_verdict(self):
        self.transport.add("POST", "/uploads", status=201, body=fakes.upload())
        self.transport.add("PUT", fakes.PUT_URL, body=b"")
        self.transport.fail("POST", COMPLETE, CuddlerError("network_error"))  # the check is slower than the request may be
        self.transport.add("GET", UPLOAD, body=fakes.upload("pending", upload_url=None))
        self.transport.add("GET", UPLOAD, body=fakes.upload("ready"))
        self.assertEqual(self.uploader.ensure(PNG, "image", "image/png", "frame.png"), fakes.UPLOAD_ID)
        self.assertEqual(len(self.transport.calls("PUT", fakes.PUT_URL)), 1)
        self.assertEqual(len(self.transport.calls("POST", "/uploads")), 1)

    def test_an_upload_missing_when_completing_is_not_blindly_started_over(self):
        self.transport.add("POST", "/uploads", status=201, body=fakes.upload())
        self.transport.add("PUT", fakes.PUT_URL, body=b"")
        self.transport.add("POST", COMPLETE, status=404, body=fakes.error("not_found", 404))
        self.transport.add("POST", COMPLETE, body=fakes.upload("ready"))
        with self.assertRaises(CuddlerError) as caught:
            self.uploader.ensure(PNG, "image", "image/png", "frame.png")
        self.assertEqual(caught.exception.code, "recovery_required")
        self.assertEqual(len(set(self.keys())), 1)

    def test_an_upload_missing_while_waiting_is_not_blindly_started_over(self):
        self.transport.add("POST", "/uploads", status=201, body=fakes.upload())
        self.transport.add("PUT", fakes.PUT_URL, body=b"")
        self.transport.add("POST", COMPLETE, body=fakes.upload("pending", upload_url=None))
        self.transport.add("POST", COMPLETE, body=fakes.upload("ready"))
        self.transport.add("GET", UPLOAD, status=404, body=fakes.error("not_found", 404))
        with self.assertRaises(CuddlerError) as caught:
            self.uploader.ensure(PNG, "image", "image/png", "frame.png")
        self.assertEqual(caught.exception.code, "recovery_required")
        self.assertEqual(len(set(self.keys())), 1)

    def test_the_same_bytes_under_another_type_are_another_upload(self):
        self.transport.add("POST", "/uploads", status=201, body=fakes.upload())
        self.transport.fail("PUT", fakes.PUT_URL, CuddlerError("network_error"))
        for content_type in ("audio/wav", "audio/x-wav"):
            with self.assertRaises(CuddlerError):
                self.uploader.ensure(WAV, "audio", content_type, "a.wav")
        self.assertEqual(len(set(self.keys())), 2)  # the Platform answers 409 to a key that returns with a new type

    def test_a_long_file_name_is_shortened(self):
        self.script_happy_path()
        self.uploader.ensure(PNG, "image", "image/png", "x" * 400 + ".png")
        sent = json.loads(self.transport.calls("POST", "/uploads")[0].body)["filename"]
        self.assertLessEqual(len(sent), 100)
        self.assertTrue(sent.endswith(".png"))

    def test_a_rejected_attempt_is_forgotten(self):
        self.transport.add("POST", "/uploads", status=201, body=fakes.upload())
        self.transport.add("PUT", fakes.PUT_URL, body=b"")
        self.transport.add("POST", COMPLETE, body=fakes.upload("rejected", rejection_reason="content_policy"))
        for _ in range(2):
            with self.assertRaises(CuddlerError):
                self.uploader.ensure(PNG, "image", "image/png", "frame.png")
        self.assertEqual(len(set(self.keys())), 2)

    def test_an_unknown_upload_response_cannot_be_replayed_with_another_key(self):
        self.transport.fail("POST", "/uploads", CuddlerError("network_error"))
        with self.assertRaises(CuddlerError):
            self.uploader.ensure(PNG, "image", "image/png", "frame.png")
        client = PlatformClient(self.transport, "cud_sk_live_" + "B" * 20)
        other = Uploader(client, self.state, sleep=lambda s: None)
        before = len(self.transport.requests)
        with self.assertRaises(CuddlerError) as caught:
            other.ensure(PNG, "image", "image/png", "frame.png")
        self.assertEqual(caught.exception.code, "recovery_required")
        self.assertEqual(len(self.transport.requests), before)

    def test_an_unbound_legacy_upload_without_an_id_stops_before_http(self):
        from cuddler_plugin.cuddler.fingerprint import digest_bytes
        digest = f"{digest_bytes(PNG)}:image:image/png"
        self.state.begin_upload(digest, "legacy-upload")
        with self.assertRaises(CuddlerError) as caught:
            self.uploader.ensure(PNG, "image", "image/png", "frame.png")
        self.assertEqual(caught.exception.code, "recovery_required")
        self.assertEqual(self.transport.requests, [])

    def test_a_ready_upload_supports_key_rotation_after_reading_the_known_id(self):
        self.script_happy_path()
        self.uploader.ensure(PNG, "image", "image/png", "frame.png")
        client = PlatformClient(self.transport, "cud_sk_live_" + "B" * 20)
        self.transport.add("GET", UPLOAD, body=fakes.upload("ready"))
        self.assertEqual(Uploader(client, self.state).ensure(PNG, "image", "image/png", "frame.png"), fakes.UPLOAD_ID)
        self.assertEqual(len(self.keys()), 1)


if __name__ == "__main__":
    unittest.main()
