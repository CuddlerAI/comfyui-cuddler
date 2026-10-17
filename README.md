# Cuddler for ComfyUI

> **In development.** This plugin is not on the Comfy Registry yet. Version 1.0 adds the
> Cuddler Video, Image, Speech and Music nodes. The current preview adds text and first-frame
> video generation; other video modes and the Image, Speech and Music nodes are still in development.

[中文说明](README.zh-CN.md)

Generate video, images, speech and music inside ComfyUI workflows with your
[Cuddler Platform](https://platform.cuddler.ai) API key. Every model in Cuddler's public
catalog is planned for four nodes, prices are shown on the node before you run, and every
run has a spending limit that Cuddler enforces.

## What you need

- ComfyUI 0.14.0 or newer.
- A Cuddler Platform API key. Create one at <https://platform.cuddler.ai/console/keys>; add funds
  at <https://platform.cuddler.ai/console/billing>.

## Install

After the 1.0 release: search for **Cuddler** in ComfyUI-Manager, or run `comfy node install cuddler`,
then restart ComfyUI. The plugin needs no extra Python packages.

## Video preview

The development version provides **Cuddler Video**. Choose a model and either Text to video or
First frame to video, write a prompt, and connect its VIDEO output to ComfyUI's **Save Video**.
For a first frame, connect one IMAGE from **Load Image**. Batches are rejected. Only the two
implemented modes are listed; reference-video, editing and other modes are not included yet.

The selector and parameters come from the catalog. A model listed publicly may still be unavailable
to your organization; the authenticated estimate and create request enforce access and spending limits.
Save the workflow with a fixed seed to recover the same operation after a restart.

## Add your API key

Open **Settings → Cuddler**, paste the key and press **Save**. The page shows the last four
characters of the key and your available balance.

On a server, set the `CUDDLER_API_KEY` environment variable instead. It takes precedence over a
key saved on the settings page.

The settings page and price labels answer only pages opened at `localhost`, an IP address or a
local name such as `my-pc.local`, so other websites cannot change your key. If you reach ComfyUI
through a domain name or a tunnel, add that name to the `CUDDLER_ALLOWED_HOSTS` environment
variable (comma separated; `.example.com` covers every name under it) and restart ComfyUI.

## Costs

- Each node shows its price before you run. Prices come from Cuddler's price estimate, which is free.
- Each node has a spending limit per run (default $2). Cuddler refuses to start a job that could
  cost more than the limit, and never charges more than it.
- Running a node again with the same inputs and the same seed reuses the earlier result for
  its retention period instead of paying again. Change the seed to get a new one. Once a result has
  expired, running it again creates a new job and charges again.
- If you stop a run while a video, speech or music job is still queued, the job is cancelled and
  nothing is charged. Image jobs and jobs that have already started cannot be cancelled: they
  finish and are charged, and running the node again collects the result.

## Privacy and security

- The key is stored in `ComfyUI/user/__cuddler/credentials.json`. ComfyUI never serves files
  from `__` folders over HTTP, and on macOS and Linux the file is readable only by your account.
- The key is never written to workflows, to image or video metadata, to ComfyUI's settings or to logs.
- `ComfyUI/user/__cuddler/state.sqlite3` keeps run fingerprints and file hashes, idempotency keys,
  job and upload ids, spending limits and opaque credential bindings. It holds no prompts or media.
  Unknown submissions retain their original key and request details for recovery. Unresolved records
  are kept until their outcome can be established; terminal jobs expire after 8 days and ready uploads
  after 29 days.
- If you change your API key, known jobs are recovered only after the replacement key successfully
  reads them. A submission without a known job id requires its original key. A missing binding,
  unreadable job or damaged database stops the run instead of creating a new paid job. Preserve the
  original records and check the Platform console or contact support; **do not delete the database to retry**.
  Keep `state.initialized` with the database: it prevents a missing database from being mistaken for a new installation.
- Every job sent to Cuddler carries the metadata `client: comfyui-cuddler/<version>`, so Cuddler
  can count jobs made from ComfyUI, and every request's User-Agent names the plugin and Python
  versions. Nothing else about you or your machine is sent. Reference files you upload keep their
  file names.
- ComfyUI has no login. Anyone who can open your ComfyUI page can spend with your key, so do not
  expose ComfyUI to a network you do not trust. The per-run limit caps each job, not total spending.
  An account daily limit applies only if one has been configured; do not assume a default total cap.
- If ComfyUI runs on another machine, a key typed on the settings page travels over that connection.
  Prefer `CUDDLER_API_KEY` there.

## Development

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -t .
npm test
```

Before a release, refresh the bundled model list with `python scripts/export_catalog_snapshot.py`.

For operator acceptance, follow [the video acceptance runbook](docs/acceptance/video-paid.md).
Its preflight reads the catalog, balance and estimate only. Real generation and settlement require
the recorded budget approval and separate evidence; host CI does not replace that acceptance.

## License

MIT, see [LICENSE](LICENSE).
