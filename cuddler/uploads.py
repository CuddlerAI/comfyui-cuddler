"""Reference media go up once: create (idempotent) -> PUT the bytes -> complete.

A ready upload is reused for the same bytes while the Platform still keeps it. An attempt that is cut off
(a dropped connection, a check slower than we wait) stays on record under its idempotency key, and the next run
continues the same upload: it asks the Platform what it already has before it sends a byte again.
"""
import time
import uuid

from .errors import CuddlerError
from .fingerprint import digest_bytes

MIB = 1024 * 1024
LIMITS = {
    "image": (30 * MIB, {"image/png", "image/jpeg", "image/webp"}),
    "video": (200 * MIB, {"video/mp4", "video/quicktime"}),
    "audio": (50 * MIB, {"audio/wav", "audio/x-wav", "audio/mpeg"}),
}
READY_WAIT_SECONDS = 60
FILENAME_CHARS = 100  # the Platform takes 200 UTF-16 units (an emoji counts two), refuses more, and keeps none


class Uploader:
    def __init__(self, client, state, *, sleep=time.sleep):
        self._client = client
        self._state = state
        self._sleep = sleep

    def ensure(self, data, media_kind, content_type, filename):
        """The id of a ready upload holding `data`.

        Raises upload_checking when the Platform is still checking the file after the wait. The attempt stays on
        record, so running again continues with the same upload instead of sending the file a second time.
        """
        limit, types = LIMITS.get(media_kind, (0, ()))
        if content_type not in types:
            raise CuddlerError("media_type_not_allowed")
        if not data or len(data) > limit:
            raise CuddlerError("media_too_large", detail={"limit": f"{limit // MIB} MiB"})
        # The type belongs to the identity: the Platform answers 409 to a key that comes back with another one.
        digest = f"{digest_bytes(data)}:{media_kind}:{content_type}"
        reused = self._reuse(digest)
        if reused:
            return reused
        for _ in range(2):  # an abandoned attempt may have expired on the Platform: start over once, with a new key
            row = self._state.upload(digest)
            identity = self._client.recovery_identity()
            if row and row.get("credential_identity") != identity:
                raise CuddlerError("recovery_required")
            key = row["idempotency_key"] if row else "upl-" + uuid.uuid4().hex
            if not row:
                self._state.begin_upload(digest, key, credential_identity=identity)
            upload = self._client.create_upload(
                {"kind": media_kind, "content_type": content_type, "bytes": len(data),
                 "filename": (filename or "")[-FILENAME_CHARS:]}, key)
            self._state.remember_upload(digest, upload["id"], credential_identity=identity)
            if upload["status"] == "pending":
                upload = self._send(upload, data, resumed=row is not None)
            if upload["status"] == "pending":
                raise CuddlerError("upload_checking")  # the row stays: the next run asks about this same upload
            if upload["status"] == "ready":
                self._state.finish_upload(digest, upload["id"])
                return upload["id"]
            self._state.drop_upload(digest)
            if upload["status"] == "rejected":
                raise CuddlerError("upload_rejected", detail={"reason": upload.get("rejection_reason") or "?"})
        raise CuddlerError("upload_expired")

    def _reuse(self, digest):
        row = self._state.upload(digest)
        if not row:
            return None
        identity = self._client.recovery_identity()
        if row["status"] != "ready" and row.get("credential_identity") == identity:
            return None
        if not row["upload_id"]:
            # An upload creation with no response cannot safely be replayed in
            # another organization, including legacy records with no binding.
            raise CuddlerError("recovery_required")
        try:
            current = self._client.get_upload(row["upload_id"])
        except CuddlerError as error:
            if error.code == "not_found":
                raise CuddlerError("recovery_required", request_id=error.request_id) from error
            raise
        if current and current["status"] == "ready":
            self._state.finish_upload(digest, row["upload_id"], credential_identity=identity)
            return row["upload_id"]
        if current["status"] == "pending":
            self._state.remember_upload(digest, row["upload_id"], credential_identity=identity)
            return None
        self._state.drop_upload(digest)
        return None

    def _send(self, upload, data, *, resumed):
        """Get the bytes to the Platform if they are not there yet, then wait for its verdict."""
        if resumed:
            # An earlier run may have got the bytes up and been cut off, or met a check that failed for a reason
            # that has passed. Ask first: sending up to 200 MiB again is the costly way to find out.
            try:
                return self._finish(upload["id"])
            except CuddlerError as error:
                if error.code != "invalid_request":  # the Platform's 400: nothing has been uploaded to this id yet
                    raise
        if not upload.get("upload_url"):
            return {"status": "expired"}  # the link ran out before any bytes arrived: this id can no longer be filled
        self._client.put_media(upload["upload_url"], upload.get("upload_headers") or {}, data)
        return self._finish(upload["id"])

    def _finish(self, upload_id):
        """Ask for the Platform's verdict, then wait up to a minute for it. Returns the upload as it stands."""
        try:
            upload = self._client.complete_upload(upload_id)
        except CuddlerError as error:
            if error.code == "not_found":
                raise CuddlerError("recovery_required", request_id=error.request_id) from error
            if error.code != "network_error":
                raise
            # No answer within the request's time, but the Platform keeps checking after the caller has gone.
            upload = {"status": "pending"}
        waited = 0
        while upload["status"] == "pending" and waited < READY_WAIT_SECONDS:
            self._sleep(1)
            waited += 1
            try:
                upload = self._client.get_upload(upload_id)
            except CuddlerError as error:
                if error.code != "not_found":
                    raise
                raise CuddlerError("recovery_required", request_id=error.request_id) from error
        return upload
