# Video acceptance: preflight, then an approved paid run

This is an operator runbook for the current text/first-frame video preview. Host CI uses a fake Platform;
real generation, file delivery and settlement need separate evidence. The script below has no generation,
upload, cancellation, credit or account-management action.

## Zero-generation preflight

1. Record the exact candidate commit and installed ComfyUI version. Create a new private run directory
   outside this checkout; keep receipts, the ledger and any state backup out of Git and chat attachments.
2. Have the authorized operator inject the temporary live key from their secret manager into
   `CUDDLER_API_KEY`, or another named environment variable. Never put its value in a command argument,
   shell history, file, screenshot or report; disable shell tracing. Do not copy a browser profile.
3. Run from this checkout, with the already approved prospective per-job ceiling:

   ```sh
   python scripts/acceptance_preflight.py \
     --output-dir "$PRIVATE_RUN_DIRECTORY" \
     --key-env CUDDLER_API_KEY \
     --model seedance-2.5 --duration 4 --resolution 720p --cap-usd 2
   ```

   `$PRIVATE_RUN_DIRECTORY` must identify the new private directory. The numeric example is a readiness
   check, not permission to spend. Override it to match the actual approval.

The script uses the plugin's client, request validation and bounded no-redirect HTTP transport. Its
allowlist is exactly `GET /v1/catalog`, `GET /v1/balance` and `POST /v1/videos/estimate` at
`https://api.cuddler.ai`. It checks the selected text-video parameters, live-key format, authenticated
read/estimate, available balance, daily headroom and the estimate's hold against the node cap.
`daily_limit_usd = "0.000000"` means no daily limit; available balance still bounds readiness. For a finite
daily limit, remaining headroom subtracts both today's captures and all in-flight holds. It writes
only a sanitized `preflight.json`, never a key, prompt, full account response or signed media URL.
On POSIX the directory/file use 0700/0600; on Windows use an operator-private user directory and verify
its inherited ACL before storing the paid evidence. It refuses to overwrite a receipt.

A pass proves those zero-generation checks at that time. It does not prove create authorization,
provider availability, output quality, a paid generation, refund or one-time capture. A 3xx, blocked
edge response, invalid key, unavailable model or failed readiness check stops here; do not follow a
redirect with credentials, switch accounts automatically or create/upload to diagnose it.

## Approved paid steps — manual only

Copy `docs/acceptance/ledger.template.json` into the private run directory. Before clicking Run, the authorized operator
must fill the approval reference, exact candidate, models/modes, maximum new jobs/uploads, total cap,
per-job cap, already spent/held and independently confirmed remaining budget. An empty field is pending.
Account balance and a passed preflight are not spending authorization. Do not reset a prior allowance,
increase credits or count a refunded attempt as a free replacement.

1. Install this exact candidate into the approved host, use the securely injected key, and connect
   **Cuddler Video → Save Video**. Start with one approved text-video operation, a fixed seed and an
   allowed model/variant. Set the node's cap no higher than the approval; inspect its current estimate.
   Click Run only after the operator has reconciled the remaining budget. Record the operation/job ID,
   estimate, hold, actual capture, capture count, saved file hash and host playback result in the private
   ledger. A successful HTTP response or balance difference alone does not prove one-time settlement.
2. With the original state and same inputs/seed, run again and restart the host before recovering once
   more. Verify the original job ID, saved media and no extra task/capture. Keep both `state.sqlite3` and
   `state.initialized`; do not delete, empty or replace them to retry. Stop on `recovery_required` and
   reconcile the earlier request with support. An unknown-ID request needs its original credential;
   changing keys is not permission to create a second task.
3. Run the first-frame case only if its additional job **and upload** are included in the approved
   scope. Use a consented non-sensitive fixture, retain the original operation mapping, and verify
   native VIDEO/Save Video output plus its task/ledger evidence. Retained results that expire may be
   generated again and charged under the product policy; do not use an expired result in a zero-new-job
   recovery case.
4. A live lost-initial-response case needs an explicitly approved, credential-safe fault-injection
   method and a job allowance. Reuse an approved existing probe if available; do not introduce an
   arbitrary proxy or authentication log. Without it, keep this case `not_run`/unverified. Closing a UI
   after receiving a task ID tests known-ID recovery, not loss of the initial response. Offline tests
   remain the evidence for key changes, damaged/missing local records and pre-submit write failures;
   do not destroy a real paid record to simulate them.
5. Reconcile original job captures/releases with the authoritative ledger and separate customer
   charges from provider cost. Fill spent/held/remaining and all unverified cases. Have the authorized
   operator revoke the temporary key, verify a read is rejected, and return unused credit only through
   an approved administrative process. This runbook/script never performs those mutations.

The public PR remains Draft and the version stays unchanged during this work. Keep raw keys, account
details, prompts, source media and usable signed URLs in their approved private stores; share a
sanitized evidence summary with the exact candidate and the last completed verification layer.
