import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from cuddler_plugin.cuddler import CLIENT_TAG
from cuddler_plugin.cuddler.client import PlatformClient
from cuddler_plugin.cuddler.errors import CuddlerError
from cuddler_plugin.cuddler.jobs import JobRunner, JobSpec, MediaInput
from cuddler_plugin.cuddler.state import StateDB
from cuddler_plugin.cuddler.uploads import Uploader
from tests import fakes

BODY = {"model": "seedance-2.5", "prompt": "an orange ball", "duration": 4, "resolution": "720p"}
ESTIMATE_BODY = {"model": "seedance-2.5", "duration": 4, "resolution": "720p"}
JOB_URL = "/videos/" + fakes.VIDEO_ID


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds
        await asyncio.sleep(0)


def make_runner(directory, transport, clock):
    state = StateDB(Path(directory))
    client = PlatformClient(transport, fakes.LIVE_KEY)
    runner = JobRunner(client, state, Uploader(client, state, sleep=lambda s: None), sleep=clock.sleep, clock=clock)
    return runner, state


def make_spec(**changes):
    values = dict(kind="video", body=dict(BODY), estimate_body=dict(ESTIMATE_BODY), cap_usd="2.000000", seed=1)
    values.update(changes)
    return JobSpec(**values)


class JobRunnerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.transport = fakes.FakeTransport()
        self.clock = FakeClock()
        self.runner, self.state = make_runner(self.tmp.name, self.transport, self.clock)
        self.events = []

    def tearDown(self):
        self.tmp.cleanup()

    def spec(self, **changes):
        return make_spec(**changes)

    async def run_job(self, spec=None, interrupted=lambda: False):
        return await self.runner.run(spec or self.spec(), interrupted=interrupted,
                                     progress=lambda event, value=None: self.events.append((event, value)))

    def script_estimate(self, hold="0.666955"):
        self.transport.add("POST", "/videos/estimate", body=fakes.estimate(hold=hold))

    def after_create(self):
        """Interrupted once the job exists: the person stops the run while it is queued or running."""
        return bool(self.transport.calls("POST", "/videos"))

    async def test_a_local_state_failure_before_the_create_records_nothing(self):
        spec = self.spec(media=[MediaInput("first_frame", b"png-bytes", "image", "image/png", "a.png")])
        with mock.patch.object(self.state, "upload", side_effect=CuddlerError("state_unavailable", status=500)):
            with self.assertRaises(CuddlerError) as caught:
                await self.run_job(spec)
        self.assertEqual(caught.exception.code, "state_unavailable")
        self.assertIsNone(self.state.job(self.runner.fingerprint(spec)))
        self.assertEqual(self.transport.calls("POST", "/videos"), [])

    async def test_a_job_made_while_its_record_could_not_be_written_is_collected_next_time(self):
        self.script_estimate()
        self.transport.add("POST", "/videos", status=202, body=fakes.video_job("queued"))
        with mock.patch.object(self.state, "update_job", side_effect=CuddlerError("state_unavailable", status=500)):
            with self.assertRaises(CuddlerError) as caught:
                await self.run_job()
        self.assertEqual(caught.exception.code, "state_unavailable")
        self.transport.routes.clear()
        self.transport.add("POST", "/videos", status=200, body=fakes.video_job("succeeded"),
                           headers={"Idempotent-Replayed": "true"})
        job = await self.run_job()
        self.assertEqual(job["id"], fakes.VIDEO_ID)
        self.assertEqual(len({r.headers["Idempotency-Key"] for r in self.transport.calls("POST", "/videos")}), 1)

    async def test_creates_once_and_waits_for_the_result(self):
        self.script_estimate()
        self.transport.add("POST", "/videos", status=202, body=fakes.video_job("queued"))
        self.transport.add("GET", JOB_URL, body=fakes.video_job("running"))
        self.transport.add("GET", JOB_URL, body=fakes.video_job("succeeded"))
        job = await self.run_job()
        self.assertEqual(job["status"], "succeeded")
        create = self.transport.calls("POST", "/videos")
        self.assertEqual(len(create), 1)
        sent = json.loads(create[0].body)
        self.assertEqual((sent["max_usd"], sent["metadata"]), ("2.000000", {"client": CLIENT_TAG}))
        self.assertEqual(self.events[0], ("queued", 3))
        self.assertEqual(self.state.job(self.runner.fingerprint(self.spec()))["status"], "succeeded")

    async def test_refuses_before_creating_when_over_the_cap(self):
        self.transport.add("POST", "/videos/estimate", body=fakes.estimate(usd="3.100000", hold="3.162000"))
        with self.assertRaises(CuddlerError) as caught:
            await self.run_job()
        self.assertEqual((caught.exception.code, caught.exception.detail),
                         ("over_cap", {"hold": "$3.17", "cap": "$2.00"}))
        self.assertEqual(self.transport.calls("POST", "/videos"), [])

    async def test_the_refusal_shows_the_hold_it_checked_not_the_smaller_estimate(self):
        self.transport.add("POST", "/videos/estimate", body=fakes.estimate(usd="1.990000", hold="2.030000"))
        with self.assertRaises(CuddlerError) as caught:
            await self.run_job()
        self.assertEqual((caught.exception.code, caught.exception.detail),
                         ("over_cap", {"hold": "$2.03", "cap": "$2.00"}))

    async def test_a_failure_before_the_create_is_priced_again_next_time(self):
        self.transport.fail("POST", "/videos/estimate", CuddlerError("network_error"))
        with self.assertRaises(CuddlerError) as caught:
            await self.run_job()
        self.assertEqual(caught.exception.code, "network_error")
        self.transport.routes.clear()
        self.transport.add("POST", "/videos/estimate", body=fakes.estimate(usd="3.100000", hold="3.162000"))
        with self.assertRaises(CuddlerError) as caught:
            await self.run_job()
        self.assertEqual(caught.exception.code, "over_cap")  # not the Platform's invalid_request
        self.assertEqual(self.transport.calls("POST", "/videos"), [])

    async def test_same_inputs_reuse_the_finished_job_without_paying_again(self):
        self.script_estimate()
        self.transport.add("POST", "/videos", status=202, body=fakes.video_job("succeeded"))
        self.transport.add("GET", JOB_URL, body=fakes.video_job("succeeded"))
        await self.run_job()
        await self.run_job()
        self.assertEqual(len(self.transport.calls("POST", "/videos")), 1)
        changed_seed = self.spec(seed=2)
        await self.run_job(changed_seed)
        self.assertEqual(len(self.transport.calls("POST", "/videos")), 2)

    async def test_an_expired_result_is_made_again(self):
        new_id = "vid_" + "B" * 20
        self.script_estimate()
        self.transport.add("POST", "/videos", status=202, body=fakes.video_job("succeeded"))
        self.transport.add("POST", "/videos", status=202, body=fakes.video_job("succeeded", job_id=new_id))
        self.transport.add("GET", JOB_URL, body=fakes.video_job("succeeded", output=None))  # its retention ended
        await self.run_job()
        job = await self.run_job()
        self.assertEqual(job["id"], new_id)
        creates = self.transport.calls("POST", "/videos")
        self.assertEqual(len(creates), 2)
        self.assertNotEqual(creates[0].headers["Idempotency-Key"], creates[1].headers["Idempotency-Key"])
        self.assertEqual(self.state.job(self.runner.fingerprint(self.spec()))["job_id"], new_id)

    async def test_another_jobs_expired_reply_cannot_trigger_a_paid_replacement(self):
        self.script_estimate()
        self.transport.add("POST", "/videos", status=202, body=fakes.video_job("succeeded"))
        self.transport.add("POST", "/videos", status=202,
                           body=fakes.video_job("succeeded", job_id="vid_" + "C" * 20))
        await self.run_job()
        fingerprint = self.runner.fingerprint(self.spec())
        original = self.state.job(fingerprint)
        # A mismatched response cannot prove expiry or access to the requested
        # task, even if it has the normal shape of an expired successful job.
        self.transport.add("GET", JOB_URL,
                           body=fakes.video_job("succeeded", job_id="vid_" + "B" * 20, output=None))
        with self.assertRaises(CuddlerError) as caught:
            await self.run_job()
        self.assertEqual(caught.exception.code, "invalid_response")
        self.assertEqual(len(self.transport.calls("POST", "/videos")), 1)
        self.assertEqual(self.state.job(fingerprint), original)

    async def test_a_lost_create_response_is_retried_with_the_same_key(self):
        self.script_estimate()
        self.transport.fail("POST", "/videos", CuddlerError("network_error"))
        self.transport.add("POST", "/videos", status=200, body=fakes.video_job("succeeded"),
                           headers={"Idempotent-Replayed": "true"})
        job = await self.run_job()
        self.assertEqual(job["id"], fakes.VIDEO_ID)
        keys = [request.headers["Idempotency-Key"] for request in self.transport.calls("POST", "/videos")]
        self.assertEqual(len(keys), 2)
        self.assertEqual(len(set(keys)), 1)

    async def test_a_lost_create_response_then_a_rate_limit_keeps_the_key(self):
        self.script_estimate()
        self.transport.fail("POST", "/videos", CuddlerError("network_error"))  # the job was made, its answer lost
        self.transport.add("POST", "/videos", status=429, body=fakes.error("rate_limited", 429))
        with self.assertRaises(CuddlerError) as caught:
            await self.run_job()
        self.assertEqual(caught.exception.code, "rate_limited")
        self.transport.routes.clear()
        self.transport.add("POST", "/videos", status=200, body=fakes.video_job("succeeded"),
                           headers={"Idempotent-Replayed": "true"})
        job = await self.run_job()
        self.assertEqual(job["id"], fakes.VIDEO_ID)
        self.assertEqual(len({r.headers["Idempotency-Key"] for r in self.transport.calls("POST", "/videos")}), 1)

    async def test_after_giving_up_the_next_run_resumes_with_the_same_key(self):
        self.script_estimate()
        self.transport.fail("POST", "/videos", CuddlerError("network_error"))
        with self.assertRaises(CuddlerError) as caught:
            await self.run_job()
        self.assertEqual(caught.exception.code, "network_error")
        first_key = self.transport.calls("POST", "/videos")[0].headers["Idempotency-Key"]
        self.transport.routes.clear()
        self.transport.add("POST", "/videos", status=200, body=fakes.video_job("succeeded"))
        await self.run_job()
        last = self.transport.calls("POST", "/videos")[-1]
        self.assertEqual(last.headers["Idempotency-Key"], first_key)
        self.assertEqual(len(self.transport.calls("POST", "/videos/estimate")), 1)  # resumed, not re-priced

    async def test_a_resumed_create_repeats_the_first_request_exactly(self):
        frame = MediaInput("first_frame", b"\x89PNG-1", "image", "image/png", "first.png")
        self.script_estimate()
        self.transport.add("POST", "/uploads", status=201, body=fakes.upload())
        self.transport.add("PUT", fakes.PUT_URL, body=b"")
        self.transport.add("POST", f"/uploads/{fakes.UPLOAD_ID}/complete", body=fakes.upload("ready"))
        self.transport.fail("POST", "/videos", CuddlerError("network_error"))
        with self.assertRaises(CuddlerError):
            await self.run_job(self.spec(media=[frame]))
        first = self.transport.calls("POST", "/videos")[0]
        self.transport.routes.clear()
        self.transport.add("POST", "/videos", status=200, body=fakes.video_job("succeeded"),
                           headers={"Idempotent-Replayed": "true"})
        # Meanwhile the limit was raised and the plugin updated: neither may change the request the key carries.
        with mock.patch("cuddler_plugin.cuddler.jobs.CLIENT_TAG", "comfyui-cuddler/9.9.9"):
            await self.run_job(self.spec(media=[frame], cap_usd="5.000000"))
        last = self.transport.calls("POST", "/videos")[-1]
        self.assertEqual((last.headers["Idempotency-Key"], last.body), (first.headers["Idempotency-Key"], first.body))
        self.assertEqual(json.loads(last.body)["first_frame"], fakes.UPLOAD_ID)

    async def test_a_resumed_create_never_carries_a_higher_limit_than_the_node(self):
        self.script_estimate()
        self.transport.fail("POST", "/videos", CuddlerError("network_error"))
        with self.assertRaises(CuddlerError):
            await self.run_job()
        key = self.transport.calls("POST", "/videos")[0].headers["Idempotency-Key"]
        with self.assertRaises(CuddlerError) as caught:
            await self.run_job(self.spec(cap_usd="1.000000"))
        self.assertEqual((caught.exception.code, caught.exception.detail),
                         ("over_cap", {"hold": "$2.00", "cap": "$1.00"}))
        self.assertEqual(len(self.transport.calls("POST", "/videos")), 3)  # the first run's three tries, no more
        row = self.state.job(self.runner.fingerprint(self.spec()))
        self.assertEqual((row["status"], row["idempotency_key"]), ("submitting", key))  # still collectable

    async def test_a_refused_create_lets_the_next_run_start_over(self):
        self.script_estimate()
        self.transport.add("POST", "/videos", status=402, body=fakes.error("insufficient_balance", 402))
        with self.assertRaises(CuddlerError):
            await self.run_job()
        self.transport.routes.clear()
        self.script_estimate()
        self.transport.add("POST", "/videos", status=202, body=fakes.video_job("succeeded"))
        await self.run_job()
        keys = [request.headers["Idempotency-Key"] for request in self.transport.calls("POST", "/videos")]
        self.assertNotEqual(keys[0], keys[1])

    async def test_failed_jobs_are_reported_and_not_reused(self):
        self.script_estimate()
        self.transport.add("POST", "/videos", status=202, body=fakes.video_job("failed"))
        with self.assertRaises(CuddlerError) as caught:
            await self.run_job()
        self.assertEqual((caught.exception.code, caught.exception.detail["reason"]),
                         ("job_failed", "The output was blocked."))
        self.assertEqual(self.state.job(self.runner.fingerprint(self.spec()))["status"], "failed")

    async def test_an_interrupt_before_the_create_sends_nothing(self):
        self.script_estimate()
        self.transport.add("POST", "/videos", status=202, body=fakes.video_job("queued"))
        stopped = lambda: bool(self.transport.calls("POST", "/videos/estimate"))  # stopped while it was priced
        with self.assertRaises(CuddlerError) as caught:
            await self.run_job(interrupted=stopped)
        self.assertEqual(caught.exception.code, "interrupted_before_create")
        self.assertEqual(self.transport.calls("POST", "/videos"), [])

    async def test_interrupting_a_queued_job_cancels_it(self):
        self.script_estimate()
        self.transport.add("POST", "/videos", status=202, body=fakes.video_job("queued"))
        self.transport.add("POST", JOB_URL + "/cancel", body=fakes.video_job("canceled"))
        with self.assertRaises(CuddlerError) as caught:
            await self.run_job(interrupted=self.after_create)
        self.assertEqual(caught.exception.code, "interrupted_canceled")
        self.assertEqual(len(self.transport.calls("POST", JOB_URL + "/cancel")), 1)
        self.assertEqual(self.state.job(self.runner.fingerprint(self.spec()))["status"], "canceled")

    async def test_interrupting_a_queued_job_that_cannot_be_canceled_leaves_it_to_be_collected_later(self):
        self.script_estimate()
        self.transport.add("POST", "/videos", status=202, body=fakes.video_job("queued"))
        self.transport.add("POST", JOB_URL + "/cancel", status=409, body=fakes.error("not_cancelable", 409))
        with self.assertRaises(CuddlerError) as caught:
            await self.run_job(interrupted=self.after_create)
        self.assertEqual(caught.exception.code, "interrupted")  # not interrupted_canceled: the job lives on
        self.assertEqual(self.state.job(self.runner.fingerprint(self.spec()))["status"], "queued")
        self.transport.add("GET", JOB_URL, body=fakes.video_job("succeeded"))
        job = await self.run_job()
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(len(self.transport.calls("POST", "/videos")), 1)

    async def test_interrupting_a_running_job_leaves_it_to_be_collected_later(self):
        self.script_estimate()
        self.transport.add("POST", "/videos", status=202, body=fakes.video_job("running"))
        with self.assertRaises(CuddlerError) as caught:
            await self.run_job(interrupted=self.after_create)
        self.assertEqual(caught.exception.code, "interrupted")
        self.assertEqual(self.transport.calls("POST", JOB_URL + "/cancel"), [])
        self.transport.add("GET", JOB_URL, body=fakes.video_job("succeeded"))
        job = await self.run_job()
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(len(self.transport.calls("POST", "/videos")), 1)

    async def test_gives_up_waiting_after_the_limit_but_keeps_the_job(self):
        self.runner._max_wait = 30
        self.script_estimate()
        self.transport.add("POST", "/videos", status=202, body=fakes.video_job("running"))
        self.transport.add("GET", JOB_URL, body=fakes.video_job("running"))
        with self.assertRaises(CuddlerError) as caught:
            await self.run_job()
        self.assertEqual(caught.exception.code, "wait_timeout")
        self.assertEqual(self.state.job(self.runner.fingerprint(self.spec()))["status"], "running")

    async def test_a_long_retry_after_does_not_outlast_the_wait_limit(self):
        self.runner._max_wait = 30
        self.script_estimate()
        self.transport.add("POST", "/videos", status=202, body=fakes.video_job("running"))
        self.transport.add("GET", JOB_URL, status=429, body=fakes.error("rate_limited", 429),
                           headers={"Retry-After": "3600"})
        with self.assertRaises(CuddlerError) as caught:
            await self.run_job()
        self.assertEqual(caught.exception.code, "wait_timeout")
        self.assertLessEqual(self.clock.now, 30)

    async def test_polling_survives_rate_limits(self):
        self.script_estimate()
        self.transport.add("POST", "/videos", status=202, body=fakes.video_job("running"))
        self.transport.add("GET", JOB_URL, status=429, body=fakes.error("rate_limited", 429), headers={"Retry-After": "20"})
        self.transport.add("GET", JOB_URL, body=fakes.video_job("succeeded"))
        job = await self.run_job()
        self.assertEqual(job["status"], "succeeded")
        self.assertGreaterEqual(self.clock.now, 20)

    async def test_media_are_uploaded_and_part_of_the_fingerprint(self):
        self.script_estimate()
        self.transport.add("POST", "/uploads", status=201, body=fakes.upload())
        self.transport.add("PUT", fakes.PUT_URL, body=b"")
        self.transport.add("POST", f"/uploads/{fakes.UPLOAD_ID}/complete", body=fakes.upload("ready"))
        self.transport.add("POST", "/videos", status=202, body=fakes.video_job("succeeded"))
        frame = MediaInput("first_frame", b"\x89PNG-1", "image", "image/png", "first.png")
        await self.run_job(self.spec(media=[frame]))
        sent = json.loads(self.transport.calls("POST", "/videos")[0].body)
        self.assertEqual(sent["first_frame"], fakes.UPLOAD_ID)
        other = MediaInput("first_frame", b"\x89PNG-2", "image", "image/png", "first.png")
        self.assertNotEqual(self.runner.fingerprint(self.spec(media=[frame])),
                            self.runner.fingerprint(self.spec(media=[other])))

    async def test_concurrent_runs_of_the_same_inputs_create_one_job(self):
        self.script_estimate()
        self.transport.add("POST", "/videos", status=202, body=fakes.video_job("succeeded"))
        self.transport.add("GET", JOB_URL, body=fakes.video_job("succeeded"))
        await asyncio.gather(self.run_job(), self.run_job())
        self.assertEqual(len(self.transport.calls("POST", "/videos")), 1)
        self.assertEqual(self.runner._locks, {})  # a finished fingerprint keeps no lock

    async def test_independent_runtimes_cannot_replace_each_others_paid_reservation(self):
        other, _ = make_runner(self.tmp.name, self.transport, self.clock)
        for _ in range(2):
            self.script_estimate()
            self.transport.add("POST", "/videos", status=202, body=fakes.video_job("succeeded"))
        ready, arrived = asyncio.Event(), 0

        def synchronize(runner):
            prepare = runner._prepare

            async def together(spec, progress):
                nonlocal arrived
                result = await prepare(spec, progress)
                arrived += 1
                if arrived == 2:
                    ready.set()
                await asyncio.wait_for(ready.wait(), 2)
                return result

            runner._prepare = together

        for runner in (self.runner, other):
            synchronize(runner)
        results = await asyncio.gather(self.run_job(), other.run(self.spec()), return_exceptions=True)
        self.assertEqual(len(self.transport.calls("POST", "/videos")), 1)
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        refused = next(r for r in results if isinstance(r, CuddlerError))
        self.assertEqual(refused.code, "recovery_required")

    def runner_with_key(self, key, *, operation_locks=None):
        client = PlatformClient(self.transport, key)
        return JobRunner(client, self.state, Uploader(client, self.state, sleep=lambda s: None),
                         sleep=self.clock.sleep, clock=self.clock, operation_locks=operation_locks)

    async def test_a_stale_terminal_reply_cannot_unlock_a_newer_paid_attempt(self):
        spec = self.spec()
        fingerprint = self.runner.fingerprint(spec)
        identity = self.runner._client.recovery_identity()
        self.state.begin_job(fingerprint, "video", "old", credential_identity=identity)
        self.state.update_job(fingerprint, expected_key="old", status="running", job_id=fakes.VIDEO_ID)
        other = StateDB(Path(self.tmp.name))
        replacement = "vid_" + "B" * 20
        send = self.transport.send

        def delayed_response(request):
            if request.method == "GET" and request.url.endswith(fakes.VIDEO_ID):
                # A second process sees the old failure and starts its replacement
                # before this process receives the delayed response for the old ID.
                other.update_job(fingerprint, expected_key="old", status="failed")
                other.begin_job(fingerprint, "video", "replacement", previous_key="old",
                                credential_identity=identity)
                other.update_job(fingerprint, expected_key="replacement", status="queued", job_id=replacement)
            return send(request)

        self.transport.send = delayed_response
        self.transport.add("GET", JOB_URL, body=fakes.video_job("failed"))
        with self.assertRaises(CuddlerError) as caught:
            await self.run_job(spec)
        self.assertEqual(caught.exception.code, "recovery_required")
        row = self.state.job(fingerprint)
        self.assertEqual((row["idempotency_key"], row["job_id"], row["status"]),
                         ("replacement", replacement, "queued"))
        self.assertEqual(self.transport.calls("POST", "/videos"), [])

    async def test_late_create_poll_and_cancel_replies_keep_the_new_attempt(self):
        for phase in ("create", "refusal", "poll", "cancel"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                transport = fakes.FakeTransport()
                runner, state = make_runner(directory, transport, FakeClock())
                other = StateDB(Path(directory))
                spec = self.spec()
                fingerprint = runner.fingerprint(spec)
                identity = runner._client.recovery_identity()
                replacement = "vid_" + "B" * 20
                if phase in {"poll", "cancel"}:
                    state.begin_job(fingerprint, "video", "old", credential_identity=identity)
                    state.update_job(fingerprint, expected_key="old", status="running", job_id=fakes.VIDEO_ID)
                send = transport.send
                reads = []

                def delayed_response(request):
                    if request.method == "GET":
                        reads.append(request)
                    replace = (phase in {"create", "refusal"} and request.url == fakes.API + "/videos"
                               or phase == "poll" and len(reads) == 2
                               or phase == "cancel" and request.url.endswith("/cancel"))
                    if replace:
                        key = other.job(fingerprint)["idempotency_key"]
                        other.update_job(fingerprint, expected_key=key, status="failed")
                        other.begin_job(fingerprint, "video", "replacement", previous_key=key,
                                        credential_identity=identity)
                        other.update_job(fingerprint, expected_key="replacement", status="queued", job_id=replacement)
                    return send(request)

                transport.send = delayed_response
                transport.add("POST", "/videos/estimate", body=fakes.estimate())
                transport.add("POST", "/videos", status=400 if phase == "refusal" else 202,
                              body=fakes.error("invalid_request", 400) if phase == "refusal"
                              else fakes.video_job("queued"))
                transport.add("GET", JOB_URL, body=fakes.video_job("queued"))
                transport.add("GET", JOB_URL, body=fakes.video_job("failed"))
                transport.add("POST", JOB_URL + "/cancel", body=fakes.video_job("canceled"))
                with self.assertRaises(CuddlerError) as caught:
                    await runner.run(spec, interrupted=lambda: phase == "cancel")
                self.assertEqual(caught.exception.code, "recovery_required")
                row = state.job(fingerprint)
                self.assertEqual((row["idempotency_key"], row["job_id"], row["status"]),
                                 ("replacement", replacement, "queued"))
                # The next actual runner attaches to the replacement; it never
                # estimates or creates a third paid operation after the stale reply.
                transport.send = send
                transport.routes.clear()
                transport.add("GET", "/videos/" + replacement,
                              body=fakes.video_job("succeeded", job_id=replacement))
                before = len(transport.calls("POST", "/videos"))
                again, _ = make_runner(directory, transport, FakeClock())
                self.assertEqual((await again.run(spec))["id"], replacement)
                self.assertEqual(len(transport.calls("POST", "/videos")), before)

    async def test_an_unknown_create_cannot_be_replayed_under_a_different_key(self):
        self.script_estimate()
        self.transport.fail("POST", "/videos", CuddlerError("network_error"))
        with self.assertRaises(CuddlerError):
            await self.run_job()  # accepted remotely, but its ID never reached this process
        original = self.transport.calls("POST", "/videos")[0]
        self.transport.routes.clear()
        before = len(self.transport.requests)
        with self.assertRaises(CuddlerError) as caught:
            await self.runner_with_key("cud_sk_live_" + "B" * 20).run(self.spec())
        self.assertEqual(caught.exception.code, "recovery_required")
        self.assertEqual(len(self.transport.requests), before)  # not even an estimate/upload
        # Restoring the original credential safely retrieves the same accepted request.
        self.transport.add("POST", "/videos", body=fakes.video_job("succeeded"))
        await self.runner_with_key(fakes.LIVE_KEY).run(self.spec())
        replay = self.transport.calls("POST", "/videos")[-1]
        self.assertEqual((replay.headers["Idempotency-Key"], replay.body),
                         (original.headers["Idempotency-Key"], original.body))

    async def test_a_known_job_allows_key_rotation_after_a_successful_read(self):
        self.script_estimate()
        self.transport.add("POST", "/videos", body=fakes.video_job("succeeded"))
        await self.run_job()
        other = self.runner_with_key("cud_sk_live_" + "B" * 20)
        self.transport.add("GET", JOB_URL, body=fakes.video_job("succeeded"))
        job = await other.run(self.spec())
        self.assertEqual(job["id"], fakes.VIDEO_ID)
        self.assertEqual(len(self.transport.calls("POST", "/videos")), 1)
        self.assertEqual(self.state.job(other.fingerprint(self.spec()))["credential_identity"],
                         other._client.recovery_identity())

    async def test_a_known_job_missing_under_a_new_key_never_turns_into_a_new_paid_job(self):
        self.script_estimate()
        self.transport.add("POST", "/videos", body=fakes.video_job("succeeded"))
        await self.run_job()
        row = self.state.job(self.runner.fingerprint(self.spec()))
        self.transport.add("GET", JOB_URL, status=404, body=fakes.error("not_found", 404))
        with self.assertRaises(CuddlerError) as caught:
            await self.runner_with_key("cud_sk_live_" + "B" * 20).run(self.spec())
        self.assertEqual(caught.exception.code, "recovery_required")
        self.assertEqual(len(self.transport.calls("POST", "/videos")), 1)
        self.assertEqual(self.state.job(self.runner.fingerprint(self.spec())), row)

    async def test_an_unbound_legacy_request_without_an_id_stops_before_any_http(self):
        fingerprint = self.runner.fingerprint(self.spec())
        self.state.begin_job(fingerprint, "video", "legacy-request", json.dumps({"max_usd": "2.000000"}))
        with self.assertRaises(CuddlerError) as caught:
            await self.run_job()
        self.assertEqual(caught.exception.code, "recovery_required")
        self.assertEqual(self.transport.requests, [])
        self.assertIsNone(self.state.job(fingerprint)["credential_identity"])

    async def test_a_legacy_job_with_an_id_can_be_bound_only_after_reading_it(self):
        fingerprint = self.runner.fingerprint(self.spec())
        self.state.begin_job(fingerprint, "video", "legacy-request")
        self.state.update_job(fingerprint, expected_key="legacy-request", status="queued", job_id=fakes.VIDEO_ID)
        self.transport.add("GET", JOB_URL, body=fakes.video_job("succeeded"))
        self.assertEqual((await self.run_job())["id"], fakes.VIDEO_ID)
        self.assertEqual(self.transport.calls("POST", "/videos"), [])
        self.assertEqual(self.state.job(fingerprint)["credential_identity"], self.runner._client.recovery_identity())

    async def test_a_missing_replay_body_is_not_treated_as_a_never_sent_request(self):
        fingerprint = self.runner.fingerprint(self.spec())
        self.state.begin_job(fingerprint, "video", "original-request", "not-json",
                             credential_identity=self.runner._client.recovery_identity())
        with self.assertRaises(CuddlerError) as caught:
            await self.run_job()
        self.assertEqual(caught.exception.code, "recovery_required")
        self.assertEqual(self.transport.requests, [])

    async def test_corruption_after_a_lost_create_response_cannot_reset_the_payment_guard(self):
        self.script_estimate()
        self.transport.fail("POST", "/videos", CuddlerError("network_error"))
        with self.assertRaises(CuddlerError):
            await self.run_job()
        original = self.transport.calls("POST", "/videos")[0]
        saved = self.state.path.read_bytes()
        damaged = b"unreadable local job records"
        self.state.path.write_bytes(damaged)
        before = len(self.transport.requests)
        with self.assertRaises(CuddlerError) as caught:
            make_runner(self.tmp.name, self.transport, self.clock)
        self.assertEqual(caught.exception.code, "recovery_required")
        self.assertEqual(self.state.path.read_bytes(), damaged)
        self.assertEqual(len(self.transport.requests), before)
        # A restored intact record still recovers the original request, rather
        # than acknowledging an empty database as a new user.
        self.state.path.write_bytes(saved)
        recovered, _ = make_runner(self.tmp.name, self.transport, self.clock)
        self.transport.routes.clear()
        self.transport.add("POST", "/videos", body=fakes.video_job("succeeded"))
        await recovered.run(self.spec())
        replay = self.transport.calls("POST", "/videos")[-1]
        self.assertEqual((replay.headers["Idempotency-Key"], replay.body),
                         (original.headers["Idempotency-Key"], original.body))

    async def test_key_rotation_during_estimation_cannot_reserve_and_charge_the_same_input_twice(self):
        self.script_estimate()
        self.transport.add("POST", "/videos", body=fakes.video_job("succeeded"))
        # The new key belongs to another organization, so it cannot read the
        # job the first key has already paid for.
        self.transport.add("GET", JOB_URL, status=404, body=fakes.error("not_found", 404))
        entered, release = threading.Event(), threading.Event()
        estimate = self.runner._client.estimate

        def blocked_estimate(*args):
            answer = estimate(*args)
            entered.set()
            if not release.wait(5):
                raise AssertionError("test estimate was not released")
            return answer

        self.runner._client.estimate = blocked_estimate
        other = self.runner_with_key("cud_sk_live_" + "B" * 20, operation_locks=self.runner._locks)
        first = asyncio.create_task(self.run_job())
        second = None
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            second = asyncio.create_task(other.run(self.spec()))
            await asyncio.sleep(0.02)
            self.assertEqual(len(self.transport.calls("POST", "/videos/estimate")), 1)
        finally:
            release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        self.assertEqual(results[0]["id"], fakes.VIDEO_ID)
        self.assertEqual(results[1].code, "recovery_required")
        self.assertEqual(len(self.transport.calls("POST", "/videos")), 1)

    async def test_a_lost_database_after_a_lost_create_response_blocks_before_any_new_http(self):
        self.script_estimate()
        self.transport.fail("POST", "/videos", CuddlerError("network_error"))
        with self.assertRaises(CuddlerError):
            await self.run_job()
        self.assertTrue(self.state.marker.exists())
        self.state.path.unlink()
        before = len(self.transport.requests)
        with self.assertRaises(CuddlerError) as caught:
            make_runner(self.tmp.name, self.transport, self.clock)
        self.assertEqual(caught.exception.code, "recovery_required")
        self.assertEqual(len(self.transport.requests), before)
        self.assertFalse(self.state.path.exists())


class EventLoopTest(unittest.TestCase):
    """ComfyUI runs every prompt with asyncio.run, so one JobRunner outlives many event loops."""

    def test_runs_of_the_same_inputs_on_a_later_loop_still_wait_for_each_other(self):
        with tempfile.TemporaryDirectory() as directory:
            transport = fakes.FakeTransport()
            runner, _state = make_runner(directory, transport, FakeClock())
            transport.add("POST", "/videos/estimate", body=fakes.estimate())
            transport.add("POST", "/videos", status=202, body=fakes.video_job("succeeded"))
            transport.add("GET", JOB_URL, body=fakes.video_job("succeeded"))

            async def prompt():
                return await asyncio.gather(runner.run(make_spec()), runner.run(make_spec()))

            for _ in range(2):
                for job in asyncio.run(prompt()):
                    self.assertEqual(job["id"], fakes.VIDEO_ID)
            self.assertEqual(len(transport.calls("POST", "/videos")), 1)


if __name__ == "__main__":
    unittest.main()
