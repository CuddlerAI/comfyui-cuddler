"""The Platform API calls the plugin makes. Every response is checked before use."""
import hashlib
import json
import re

from .errors import CuddlerError
from .transport import Request, checked_https_url

ORIGIN = "https://api.cuddler.ai"
JOB_PATHS = {"video": "videos", "image": "images", "speech": "speech", "music": "music"}
JOB_PREFIXES = {"video": "vid", "image": "img", "speech": "aud", "music": "mus"}
ESTIMATE_OBJECTS = {"video": "estimate", "image": "image_estimate",
                    "speech": "speech_estimate", "music": "music_estimate"}
# The Platform has POST /{videos,speech,music}/{id}/cancel and no such route for images.
CANCELABLE = {"video", "speech", "music"}
JOB_STATUSES = {"queued", "running", "succeeded", "failed", "canceled"}
UPLOAD_STATUSES = {"pending", "ready", "rejected", "expired"}
ERROR_CODES = {"invalid_request", "unauthorized", "insufficient_balance", "forbidden",
               "model_not_enabled", "organization_suspended", "not_found", "idempotency_conflict",
               "not_cancelable", "content_blocked", "rate_limited", "queue_full",
               "daily_limit_reached", "internal_error", "upstream_error", "unavailable"}
# The layers in front of the Platform (Cloudflare, the load balancer) answer with a page of their own
# when they refuse a request. For these statuses it means what the Platform's own error would mean.
EDGE_STATUS_CODES = {401: "unauthorized", 429: "rate_limited"}
_MONEY = re.compile(r"-?(?:0|[1-9][0-9]{0,8})\.[0-9]{6}")  # exactly what money.to_micros() reads
_RETRY_AFTER = re.compile(r"[0-9]{1,9}")  # plain seconds: int() raises on other digits and on huge strings
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9._:-]{8,128}")
_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
MAX_JSON = 4 * 1024 * 1024


def _one_of(value, allowed):
    """`value in allowed` for a value out of a response: a list or dict there must not raise TypeError."""
    return isinstance(value, str) and value in allowed


def _header(response, name):
    value = response.headers.get(name)
    return value.strip() if isinstance(value, str) else ""


def _idempotency_key(value):
    """The key a create must carry (the Platform answers 400 without one), checked before anything is sent."""
    if not isinstance(value, str) or not _IDEMPOTENCY_KEY.fullmatch(value):
        raise CuddlerError("invalid_request")
    return value


def public_id(value, prefix):
    if not isinstance(value, str) or not re.fullmatch(prefix + r"_[A-Za-z0-9]{12,40}", value):
        raise CuddlerError("invalid_response")
    return value


def money(value):
    if not isinstance(value, str) or not _MONEY.fullmatch(value):
        raise CuddlerError("invalid_response")
    return value


def _path(kind):
    if kind not in JOB_PATHS:
        raise CuddlerError("invalid_request")
    return JOB_PATHS[kind]


def _job(kind, value):
    if value.get("object") != kind or not _one_of(value.get("status"), JOB_STATUSES):
        raise CuddlerError("invalid_response")
    public_id(value.get("id"), JOB_PREFIXES[kind])
    estimate = value.get("estimate")
    if not isinstance(estimate, dict):
        raise CuddlerError("invalid_response")
    money(estimate.get("usd"))
    money(estimate.get("hold_usd"))
    if value["status"] == "succeeded":
        output = value.get("output")
        if output is None:
            # Valid per the contract (`output` is nullable): the Platform deletes the file when its
            # retention ends and the job stays "succeeded". Not a malformed response.
            raise CuddlerError("output_expired")
        if not isinstance(output, dict) or not isinstance(output.get("url"), str):
            raise CuddlerError("invalid_response")
    if value["status"] == "failed" and not isinstance(value.get("error"), dict):
        raise CuddlerError("invalid_response")
    return value


def _upload(value):
    if value.get("object") != "upload" or not _one_of(value.get("status"), UPLOAD_STATUSES):
        raise CuddlerError("invalid_response")
    public_id(value.get("id"), "upl")
    return value


class PlatformClient:
    def __init__(self, transport, key=None, *, origin=ORIGIN):
        self._transport = transport
        self._key = key
        self.origin = origin.rstrip("/")

    def recovery_identity(self):
        """A local credential binding; never a claim about the key's organization.

        Unknown submissions may only be replayed with the credential that sent them.
        A replacement key can recover a known job by successfully reading it first.
        Only this one-way digest is stored with recovery records, never the key.
        """
        if not self._key:
            raise CuddlerError("key_missing")
        value = json.dumps(["cuddler-recovery-v1", self.origin, self._key],
                           separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(value).hexdigest()

    def _call(self, method, path, body=None, *, idempotency_key=None, auth=True, timeout=30.0):
        headers = {"Accept": "application/json"}
        if auth:
            if not self._key:
                raise CuddlerError("key_missing")
            headers["Authorization"] = "Bearer " + self._key
        data = None
        if body is not None:
            try:
                data = json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
            except (TypeError, ValueError, RecursionError):
                raise CuddlerError("invalid_request") from None
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = _idempotency_key(idempotency_key)
        response = self._transport.send(
            Request(method, self.origin + "/v1" + path, headers, data, MAX_JSON, timeout))
        try:
            value = json.loads(response.body.decode("utf-8")) if response.body else None
        except (UnicodeDecodeError, ValueError, RecursionError):
            value = None  # not JSON, or nested so deeply that the parser gave up
        if response.status >= 400:
            raise _error(response, value)
        if response.status not in (200, 201, 202) or not isinstance(value, dict):
            raise CuddlerError("invalid_response", status=response.status)
        return value, response

    # Read-only calls.
    def catalog(self, *, timeout=30.0):
        value, _ = self._call("GET", "/catalog", auth=False, timeout=timeout)
        if value.get("object") != "catalog" or not isinstance(value.get("models"), list):
            raise CuddlerError("invalid_response")
        return value

    def balance(self):
        value, _ = self._call("GET", "/balance")
        if value.get("object") != "balance":
            raise CuddlerError("invalid_response")
        for name in ("total_usd", "held_usd", "available_usd", "daily_limit_usd", "spent_today_usd"):
            money(value.get(name))
        return value

    def estimate(self, kind, body):
        value, _ = self._call("POST", f"/{_path(kind)}/estimate", body)
        if value.get("object") != ESTIMATE_OBJECTS[kind]:
            raise CuddlerError("invalid_response")
        money(value.get("usd"))
        money(value.get("hold_usd"))
        return value

    # Jobs.
    def create(self, kind, body, idempotency_key):
        """Returns (job, replayed). A replay is the job an earlier request with this key created."""
        value, response = self._call("POST", "/" + _path(kind), body,
                                     idempotency_key=_idempotency_key(idempotency_key))
        replayed = _header(response, "idempotent-replayed").lower() == "true"
        return _job(kind, value), replayed

    def get(self, kind, job_id):
        """The job. A succeeded job whose file was deleted after its retention raises `output_expired`."""
        path = f"/{_path(kind)}/{public_id(job_id, JOB_PREFIXES[kind])}"
        value = self._call("GET", path)[0]
        # Expiry can authorize a new paid attempt. It is trustworthy only after
        # the reply has been bound to the exact task the caller asked to recover.
        if value.get("id") != job_id:
            raise CuddlerError("invalid_response")
        return _job(kind, value)

    def cancel(self, kind, job_id):
        """Cancel a queued job. Images have no cancel route, so they answer `not_cancelable` without a request."""
        base = _path(kind)
        if kind not in CANCELABLE:
            raise CuddlerError("not_cancelable")
        path = f"/{base}/{public_id(job_id, JOB_PREFIXES[kind])}/cancel"
        value = self._call("POST", path)[0]
        if value.get("id") != job_id:
            raise CuddlerError("invalid_response")
        return _job(kind, value)

    # Uploads.
    def create_upload(self, body, idempotency_key):
        return _upload(self._call("POST", "/uploads", body,
                                  idempotency_key=_idempotency_key(idempotency_key))[0])

    def complete_upload(self, upload_id):
        return _upload(self._call("POST", f"/uploads/{public_id(upload_id, 'upl')}/complete")[0])

    def get_upload(self, upload_id):
        return _upload(self._call("GET", f"/uploads/{public_id(upload_id, 'upl')}")[0])

    def put_media(self, url, headers, data):
        checked_https_url(url, media=True)
        if not isinstance(headers, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
            raise CuddlerError("invalid_response")
        response = self._transport.send(Request("PUT", url, dict(headers), data, 64 * 1024, 300.0))
        if not 200 <= response.status < 300:
            raise CuddlerError("media_transfer_failed", status=response.status)

    def download(self, url, sink, *, limit):
        """Write a signed result link into `sink` (an open binary file), at most `limit` bytes.

        What writing to `sink` raises (disk full, closed file) propagates unchanged, and after any
        error `sink` may hold part of the file.
        """
        checked_https_url(url, media=True)
        response = self._transport.stream(Request("GET", url, {}, None, limit, 300.0), sink)
        if response.status != 200:
            raise CuddlerError("media_transfer_failed", status=response.status)


def _error(response, value):
    retry = _header(response, "retry-after")
    retry_after = min(int(retry), 86_400) if _RETRY_AFTER.fullmatch(retry) else None
    code = request_id = None
    if isinstance(value, dict):
        code = value["code"] if _one_of(value.get("code"), ERROR_CODES) else None
        request_id = value.get("request_id")
    if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
        request_id = _header(response, "x-request-id")
        if not _REQUEST_ID.fullmatch(request_id):
            request_id = None
    if code is None:
        code = ("platform_unavailable" if response.status >= 500
                else EDGE_STATUS_CODES.get(response.status, "invalid_response"))
    return CuddlerError(code, status=response.status, request_id=request_id, retry_after=retry_after)
