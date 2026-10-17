"""Run one paid job: check the price against the cap, create it once, wait for it.

The same inputs (fingerprint) never create a second job while the first one is
queued, running or succeeded: the runner attaches to it instead.

The idempotency key goes on record right before the first create request that
carries it, together with everything that request adds to the inputs: the
upload ids, max_usd and the client tag. Until the Platform answers that request
itself, every later run sends exactly the same request again, without a new
price check, and the Platform answers it with the job it may already have made.
A changed body (another limit, a re-upload, a newer plugin) would instead be
refused with idempotency_conflict while the first job is charged.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import json
import time
import uuid

from . import CLIENT_TAG
from .errors import CuddlerError
from .fingerprint import digest_bytes, run_fingerprint
from .money import display, to_micros

ACTIVE = {"queued", "running"}
# Worth asking again: no answer, or a failure on the Platform's side.
# state_unavailable: the local record could not be read or written.
RETRYABLE = {"network_error", "internal_error", "upstream_error", "unavailable", "platform_unavailable",
             "state_unavailable"}
# The Platform's own refusals of a create request. It looks the idempotency key up before it checks any of these,
# so they mean no job carries the key, even when an earlier request with it went unanswered: the next run prices the
# inputs again under a new key. Any other failure (no answer, a 5xx, a 429 or an error page from the edge, sign-in,
# idempotency_conflict) keeps the key on record, because an earlier request may have made the job.
REFUSED = {"invalid_request", "insufficient_balance", "model_not_enabled", "content_blocked", "queue_full",
           "daily_limit_reached"}


@dataclass
class MediaInput:
    api_field: str                   # "first_frame", "reference_images", ...
    data: bytes = field(repr=False)
    media_kind: str                  # "image", "video" or "audio"
    content_type: str
    filename: str
    many: bool = False               # True for list fields such as reference_images


@dataclass
class JobSpec:
    kind: str                        # "video", "image", "speech" or "music"
    body: dict                       # create body without media, max_usd or metadata
    estimate_body: dict              # the /estimate body for the same request
    cap_usd: str                     # max_usd, e.g. "2.000000"
    seed: int = 0
    media: list = field(default_factory=list)


def _request_body(body, added):
    """`body` plus what the runner adds to it. List fields (reference_images) are extended, never shared."""
    request = dict(body)
    for name, value in added.items():
        request[name] = [*request.get(name, []), *value] if isinstance(value, list) else value
    return request


def _unanswered(row, kind):
    """What an earlier create request, sent and never answered, added to the inputs; None when there was none.

    A row without that record cannot be repeated exactly. Its caller must stop
    rather than treating an unanswered request as never sent.
    """
    if not row or row["status"] != "submitting" or row["kind"] != kind or not row.get("request"):
        return None
    try:
        added = json.loads(row["request"])
    except (ValueError, TypeError, RecursionError):
        return None
    return added if isinstance(added, dict) and isinstance(added.get("max_usd"), str) else None


class JobRunner:
    def __init__(self, client, state, uploader, *, sleep=asyncio.sleep, clock=time.monotonic,
                 poll_first=2.0, poll_max=15.0, max_wait=3600.0, create_attempts=3, operation_locks=None):
        self._client = client
        self._state = state
        self._uploader = uploader
        self._sleep = sleep
        self._clock = clock
        self._poll_first = poll_first
        self._poll_max = poll_max
        self._max_wait = max_wait
        self._create_attempts = create_attempts
        # A runtime shares this table across credential-bound runners: a key
        # change during estimation must not reserve the same input twice.
        self._locks = {} if operation_locks is None else operation_locks

    def fingerprint(self, spec):
        media = {}
        for item in spec.media:
            digest = digest_bytes(item.data)
            if item.many:
                media.setdefault(item.api_field, []).append(digest)
            else:
                media[item.api_field] = digest
        return run_fingerprint(spec.kind, spec.body, media, spec.seed)

    async def run(self, spec, *, interrupted=lambda: False, progress=lambda event, value=None: None):
        """The succeeded job. `progress` gets ("uploading"), ("queued", position) or ("running", seconds)."""
        fingerprint = await asyncio.to_thread(self.fingerprint, spec)  # hashes up to 200 MiB of media
        async with self._lock(fingerprint):
            job = await self._attach(spec.kind, fingerprint)
            if job is None:
                job = await self._submit(spec, fingerprint, interrupted, progress)
            return await self._wait(spec.kind, fingerprint, job, interrupted, progress)

    @asynccontextmanager
    async def _lock(self, fingerprint):
        """One run of a fingerprint at a time. A lock is kept only while runs hold or wait for it: ComfyUI runs every
        prompt on a new event loop, and an asyncio lock that once made a run wait belongs to that loop."""
        entry = self._locks.setdefault(fingerprint, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if not entry[1]:
                del self._locks[fingerprint]

    async def _state_call(self, method, *args, **kwargs):
        """SQLite may wait seconds for a lock: never on the event loop, where other nodes poll and watch for stops."""
        return await asyncio.to_thread(method, *args, **kwargs)

    async def _attach(self, kind, fingerprint):
        row = await self._state_call(self._state.job, fingerprint)
        if not row:
            return None
        if row["kind"] != kind:
            raise CuddlerError("recovery_required")
        identity = self._client.recovery_identity()
        if not row["job_id"]:
            return None
        if row["status"] not in ACTIVE | {"succeeded"} and row.get("credential_identity") == identity:
            return None
        try:
            job = await asyncio.to_thread(self._client.get, kind, row["job_id"])
        except CuddlerError as error:
            if error.code == "not_found":
                # A different organization also sees 404. It does not prove
                # that the earlier task never existed or was not charged.
                raise CuddlerError("recovery_required", request_id=error.request_id) from error
            if error.code != "output_expired":
                raise
            # An authenticated succeeded job with a null output proves access
            # and expiry. Re-generating it is the existing paid product policy.
            await self._state_call(self._state.update_job, fingerprint, status="failed",
                                   credential_identity=identity)
            return None
        # Reading a known task proves this key can access its organization.
        # This permits ordinary key rotation without guessing an org ID.
        await self._state_call(self._state.update_job, fingerprint, status=job["status"],
                               credential_identity=identity)
        return job

    async def _submit(self, spec, fingerprint, interrupted, progress):
        row = await self._state_call(self._state.job, fingerprint)
        identity = self._client.recovery_identity()
        if row and (row["kind"] != spec.kind or row.get("credential_identity") != identity):
            raise CuddlerError("recovery_required")
        added = _unanswered(row, spec.kind)
        if row and (row["status"] not in {"failed", "canceled", "submitting"}
                    or row["status"] == "submitting" and added is None):
            raise CuddlerError("recovery_required")
        if added is None:
            key = None
            added = await self._prepare(spec, progress)
        else:
            # Repeat the unanswered request exactly. Its price was checked against the limit it carries, which must
            # not be above the one the node shows now.
            key = row["idempotency_key"]
            if to_micros(added["max_usd"]) > to_micros(spec.cap_usd):
                raise CuddlerError("over_cap", detail={"hold": display(added["max_usd"]),
                                                       "cap": display(spec.cap_usd, down=True)})
        if interrupted():
            raise CuddlerError("interrupted_before_create")  # nothing that costs money was sent
        if key is None:
            key = "job-" + uuid.uuid4().hex
            await self._state_call(self._state.begin_job, fingerprint, spec.kind, key, json.dumps(added),
                                   credential_identity=identity)
        try:
            job = await self._create(spec.kind, _request_body(spec.body, added), key)
        except CuddlerError as error:
            if error.code in REFUSED:
                await self._state_call(self._state.update_job, fingerprint, status="failed")  # no job has this key
            raise
        await self._state_call(self._state.update_job, fingerprint, status=job["status"], job_id=job["id"])
        return job

    async def _prepare(self, spec, progress):
        """Upload the media and check the price. Returns what the create request adds to `spec.body`."""
        added = {}
        for item in spec.media:
            progress("uploading")
            upload_id = await asyncio.to_thread(
                self._uploader.ensure, item.data, item.media_kind, item.content_type, item.filename)
            if item.many:
                added.setdefault(item.api_field, []).append(upload_id)
            else:
                added[item.api_field] = upload_id
        quote = await asyncio.to_thread(self._client.estimate, spec.kind, spec.estimate_body)
        if to_micros(quote["hold_usd"]) > to_micros(spec.cap_usd):
            # The Platform refuses when the hold (the most a job can be charged) is above max_usd, so
            # show the hold: the estimate can sit under the limit while the hold is over it.
            raise CuddlerError("over_cap", detail={"hold": display(quote["hold_usd"]),
                                                   "cap": display(spec.cap_usd, down=True)})
        added["max_usd"] = spec.cap_usd
        added["metadata"] = {"client": CLIENT_TAG}
        return added

    async def _create(self, kind, body, key):
        for attempt in range(self._create_attempts):
            try:
                job, _replayed = await asyncio.to_thread(self._client.create, kind, body, key)
                return job
            except CuddlerError as error:
                if error.code not in RETRYABLE or attempt == self._create_attempts - 1:
                    raise
                await self._sleep(min(2 ** attempt, 8))

    async def _wait(self, kind, fingerprint, job, interrupted, progress):
        started = self._clock()
        delay = self._poll_first
        while job["status"] in ACTIVE:
            if interrupted():
                await self._stop(kind, fingerprint, job)
            waited = self._clock() - started
            if waited >= self._max_wait:
                raise CuddlerError("wait_timeout")
            if job["status"] == "queued":
                progress("queued", job.get("queue_position"))
            else:
                progress("running", int(waited))
            await self._pause(min(delay, self._max_wait - waited), interrupted)  # a long Retry-After included
            delay = min(delay * 1.5, self._poll_max)
            try:
                job = await asyncio.to_thread(self._client.get, kind, job["id"])
            except CuddlerError as error:
                if error.code not in RETRYABLE | {"rate_limited"}:
                    raise
                delay = max(delay, float(error.retry_after or 0))
                continue
            await self._state_call(self._state.update_job, fingerprint, status=job["status"], job_id=job["id"])
        if job["status"] == "succeeded":
            return job
        await self._state_call(self._state.update_job, fingerprint, status=job["status"])
        if job["status"] == "canceled":
            raise CuddlerError("job_canceled")
        failure = job.get("error") or {}
        raise CuddlerError("job_failed", detail={"reason": failure.get("message") or failure.get("code") or "?"})

    async def _pause(self, seconds, interrupted):
        end = self._clock() + seconds
        while not interrupted():
            remaining = end - self._clock()
            if remaining <= 0:
                return
            await self._sleep(min(0.5, remaining))

    async def _stop(self, kind, fingerprint, job):
        """Stop waiting. A queued job is canceled when the Platform lets us; any other job keeps running."""
        if job["status"] == "queued":
            try:
                job = await asyncio.to_thread(self._client.cancel, kind, job["id"])
                await self._state_call(self._state.update_job, fingerprint, status=job["status"])
            except CuddlerError as error:
                if error.code not in RETRYABLE | {"not_cancelable"}:  # not cancelable: it started, or an image
                    raise
            else:
                if job["status"] == "canceled":
                    raise CuddlerError("interrupted_canceled")
        raise CuddlerError("interrupted")  # not canceled: the job keeps running and can be collected
