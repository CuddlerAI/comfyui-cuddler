"""Zero-generation acceptance preflight. Reads a key reference; never creates or uploads.

    python scripts/acceptance_preflight.py --output-dir ~/.local/state/cuddler-acceptance/run-1

Only catalog, balance and video estimate requests can leave this script. A passed
preflight does not approve a budget or prove a real generated result/settlement.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
if "cuddler_plugin" not in sys.modules:
    package = types.ModuleType("cuddler_plugin")
    package.__path__ = [str(ROOT)]
    sys.modules["cuddler_plugin"] = package

from cuddler_plugin.cuddler import CLIENT_TAG  # noqa: E402
from cuddler_plugin.cuddler.catalog import parse  # noqa: E402
from cuddler_plugin.cuddler.client import ORIGIN, PlatformClient  # noqa: E402
from cuddler_plugin.cuddler.credentials import KEY  # noqa: E402
from cuddler_plugin.cuddler.errors import CuddlerError  # noqa: E402
from cuddler_plugin.cuddler.files import ensure_private_dir, write_private  # noqa: E402
from cuddler_plugin.cuddler.money import to_micros  # noqa: E402
from cuddler_plugin.cuddler.transport import HttpTransport  # noqa: E402
from cuddler_plugin.cuddler.video import choices, prepare  # noqa: E402

ALLOWED = {("GET", ORIGIN + "/v1/catalog"), ("GET", ORIGIN + "/v1/balance"),
           ("POST", ORIGIN + "/v1/videos/estimate")}


class PreflightTransport:
    """An exact allowlist in addition to the existing bounded, no-redirect transport."""
    def __init__(self, transport):
        self._transport = transport

    def send(self, request):
        if (request.method, request.url) not in ALLOWED:
            raise CuddlerError("preflight_write_refused")
        response = self._transport.send(request)
        if 300 <= response.status < 400:
            raise CuddlerError("redirect_refused")
        return response

    def stream(self, request, sink):
        raise CuddlerError("preflight_write_refused")


def preflight(key, transport, args):
    if not key:
        raise CuddlerError("key_missing")
    if not KEY.fullmatch(key):
        raise CuddlerError("key_invalid_format")
    if not key.startswith("cud_sk_live_"):
        raise CuddlerError("test_key")
    client = PlatformClient(PreflightTransport(transport), key)
    catalog = parse(client.catalog())
    selected = next(((label, model) for label, (model, mode, variant) in choices(catalog).items()
                     if model["id"] == args.model and mode == "text" and variant == args.variant), None)
    if selected is None:
        raise CuddlerError("model_unavailable")
    label, model = selected
    parameters = {"duration": args.duration, "resolution": args.resolution, "aspect_ratio": args.ratio}
    if "generate_audio" in model["request_fields"]:
        parameters["generate_audio"] = args.generate_audio
    # Reuse node validation. The fixed placeholder prompt stays in memory and
    # is excluded by prepare() from the estimate body sent over the network.
    spec = prepare(catalog, label, parameters, "acceptance preflight only", 0, args.cap_usd)
    balance = client.balance()
    # The client already validates the object and all five fixed-six-place
    # decimal strings. Complete the balance envelope without duplicating the
    # catalog/amount parser or treating the no-limit sentinel as zero budget.
    if (balance.get("currency") != "usd" or "expires_at" not in balance
            or balance["expires_at"] is not None and not isinstance(balance["expires_at"], str)):
        raise CuddlerError("invalid_response")
    daily_limit = to_micros(balance["daily_limit_usd"])
    spent_today = to_micros(balance["spent_today_usd"])
    held = to_micros(balance["held_usd"])
    if min(daily_limit, spent_today, held) < 0:
        raise CuddlerError("invalid_response")
    quote = client.estimate("video", spec.estimate_body)
    hold = to_micros(quote["hold_usd"])
    if min(hold, to_micros(quote["usd"])) < 0:
        raise CuddlerError("invalid_response")
    checks = {"catalog_choice_valid": True, "authenticated_balance": True, "authenticated_estimate": True,
              "cap_covers_hold": hold <= to_micros(spec.cap_usd),
              "available_balance_covers_hold": hold <= to_micros(balance["available_usd"]),
              "account_daily_remaining_covers_hold": daily_limit == 0 or hold <= daily_limit - spent_today - held}
    return {"status": "passed" if all(checks.values()) else "blocked", "checks": checks,
            "model": args.model, "mode": "text", "variant": args.variant,
            "parameters": parameters, "cap_usd": spec.cap_usd,
            "estimate": {"usd": quote["usd"], "hold_usd": quote["hold_usd"]}}


def main(argv=None, *, environ=None, transport=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, help="new private run directory outside this checkout")
    parser.add_argument("--key-env", default="CUDDLER_API_KEY", help="environment variable NAME, never a key value")
    parser.add_argument("--model", default="seedance-2.5")
    parser.add_argument("--variant", default="default")
    parser.add_argument("--duration", type=int, default=4)
    parser.add_argument("--resolution", default="720p")
    parser.add_argument("--ratio", default="16:9")
    parser.add_argument("--generate-audio", action="store_true")
    parser.add_argument("--cap-usd", type=float, default=2.0, help="readiness check only; not budget approval")
    args = parser.parse_args(argv)
    report = {"schema_version": 1, "phase": "zero_generation_preflight", "client": CLIENT_TAG,
              "created_at": datetime.now(timezone.utc).isoformat(), "paid_requests_sent": 0,
              "uploads_sent": 0, "budget_approval": "not_verified", "paid_acceptance": "not_started"}
    target = None
    try:
        directory = Path(args.output_dir).expanduser().resolve()
        if directory == ROOT or ROOT in directory.parents:
            raise CuddlerError("unsafe_output_path")
        target = directory / "preflight.json"
        if target.exists():
            target = None  # never overwrite an earlier acceptance receipt
            raise CuddlerError("output_exists")
        ensure_private_dir(directory)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.key_env):
            raise CuddlerError("invalid_key_reference")
        environment = os.environ if environ is None else environ
        key = (environment.get(args.key_env) or "").strip()
        report.update(preflight(key, transport or HttpTransport(CLIENT_TAG + " (acceptance preflight)"), args))
    except CuddlerError as error:
        report.update(status="blocked", error_code=error.code)
    except Exception:
        # No exception bodies/tracebacks: an OS/proxy/server error may contain
        # credentials, account information or a signed URL.
        report.update(status="blocked", error_code="preflight_failed")
    if target is not None:
        try:
            write_private(target, (json.dumps(report, indent=2, sort_keys=True) + "\n").encode())
        except Exception:
            report.update(status="blocked", error_code="private_report_unwritable")
    summary = {name: report[name] for name in ("status", "paid_requests_sent", "uploads_sent")}
    if "error_code" in report:
        summary["error_code"] = report["error_code"]
    print(json.dumps(summary))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
