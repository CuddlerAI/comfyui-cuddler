"""One runtime per ComfyUI process: the key store, local state, the catalog and the HTTP transport."""
import logging
from pathlib import Path
import platform
import threading

from ..cuddler import CLIENT_TAG
from ..cuddler.catalog import CatalogStore
from ..cuddler.client import PlatformClient
from ..cuddler.credentials import KeyStore
from ..cuddler.errors import CuddlerError
from ..cuddler.files import ensure_private_dir
from ..cuddler.jobs import JobRunner
from ..cuddler.uploads import Uploader
from ..cuddler.i18n import language_from_settings
from ..cuddler.state import StateDB
from ..cuddler.transport import HttpTransport

log = logging.getLogger("cuddler")
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = PACKAGE_ROOT / "data" / "catalog_snapshot.json"


def _private_folder(directory):
    """Create the private folder, or fail the way the rest of the plugin reports an unusable folder.

    StateDB converts its own file-system failures to `state_unavailable`, but it runs after this call.
    Without the conversion a read-only, foreign-owned or occupied folder escapes as a bare OSError:
    the extension's on_load only catches CuddlerError, so ComfyUI would drop the whole node pack.
    """
    try:
        return ensure_private_dir(directory)
    except OSError as error:
        raise CuddlerError("state_unavailable", status=500,
                           detail={"reason": error.strerror or type(error).__name__}) from error


class Runtime:
    def __init__(self, directory, *, settings_path, snapshot_path=SNAPSHOT, transport=None, environ=None):
        self.directory = _private_folder(directory)
        self.transport = transport or HttpTransport(f"{CLIENT_TAG} (Python {platform.python_version()})")
        self.keys = KeyStore(self.directory, environ)
        self.state = StateDB(self.directory)
        self.catalog = CatalogStore(snapshot_path, self.directory, self._fetch_catalog)
        self._settings_path = settings_path
        self._runners = {}
        self._runner_lock = threading.Lock()
        self._job_locks = {}  # fingerprint locks span credential changes, not just one client

    def _fetch_catalog(self):
        return PlatformClient(self.transport).catalog(timeout=5)

    def client(self, *, live=False):
        """A client holding the configured key. `live=True` refuses test keys (they cannot create jobs)."""
        key = self.keys.resolve()
        if key is None:
            raise CuddlerError("key_missing")
        if live and not key.startswith("cud_sk_live_"):
            raise CuddlerError("test_key")
        return PlatformClient(self.transport, key)

    def language(self):
        return language_from_settings(self._settings_path)

    def job_runner(self, client):
        """Share per-operation locks across nodes using the same credential."""
        identity = client.recovery_identity()
        with self._runner_lock:
            if identity not in self._runners:
                self._runners[identity] = JobRunner(client, self.state, Uploader(client, self.state),
                                                    operation_locks=self._job_locks)
            return self._runners[identity]

    def refresh_in_background(self):
        """Tidy old records and refresh the catalog without delaying ComfyUI's start.

        The two jobs are independent: a locked or unwritable job database must not keep the model list
        from updating, and each failure is logged under its own name.
        """
        def attempt(what, action):
            try:
                return action()
            except CuddlerError as error:
                log.warning("cuddler: %s skipped (%s)", what, error.code)
            except Exception as error:  # never let a background job take ComfyUI down
                log.warning("cuddler: %s failed (%s)", what, type(error).__name__)

        def work():
            attempt("tidying old job records", self.state.cleanup)
            if attempt("model list refresh", self.catalog.refresh):
                log.info("cuddler: model list updated; new models appear after the next restart")

        thread = threading.Thread(target=work, name="cuddler-refresh", daemon=True)
        thread.start()
        return thread


_runtime = None
_transport = None
_lock = threading.Lock()


def configure_transport(transport):
    """Route every Platform call through `transport` (the host tests use a fake Platform)."""
    global _runtime, _transport
    with _lock:
        _transport = transport
        _runtime = None


def get_runtime():
    """The process-wide runtime. Raises CuddlerError("state_unavailable") when the private folder cannot
    be used; that failure is not remembered, so the next call tries again (fixing the folder is enough)."""
    global _runtime
    with _lock:
        if _runtime is None:
            from . import paths
            _runtime = Runtime(paths.private_directory(), settings_path=paths.comfy_settings_file(),
                               transport=_transport)
        return _runtime
