# Harrison Deals submit (email + password)

Self-contained Flask app. Users must register with an email and password before posting. Each account can list, edit, and delete only its own deals.

## Run on Windows (`hohoho`)

1. Copy this folder to `C:\Users\Admin\harrison-deals-submit\`
2. Double-click `serve.bat`, or in Command Prompt:

   ```bat
   cd /d C:\Users\Admin\harrison-deals-submit
   serve.bat
   ```

3. The app listens on `0.0.0.0:8788`. Point the existing cloudflared tunnel at that port (same public URL as before, new local port).
4. Optional environment variables:
   - `SECRET_KEY` — session signing key. If unset, a key is created at `data\secret_key` and reused.
   - `HD_PUBLIC_BASE` — public base URL (the Cloudflare tunnel URL, no trailing slash) so uploaded covers get a full `cover_url`.
   - `HD_SUBMIT_HOST` / `HD_SUBMIT_PORT` — default `0.0.0.0` and `8788`.
   - `HD_PERSIST_ROOT` — parent for `data\` and `uploads\`. Leave unset on a local PC (defaults to this folder).

First run creates an empty SQLite database at `data\app.db` (no seed users). Accounts are created via `/register`.

## What it writes

New, edited, and deleted deals also write a JSON file into `inbox\` next to `app.py`, in the shape the Notion drain already reads (`submitter` is the logged-in email). Cover uploads land under `uploads\<user_id>\` inside `HD_PERSIST_ROOT` (the repo folder when that variable is unset).

## Persistent disk (Railway)

Seller covers and the SQLite files (`app.db`, `seed_overrides.json`, deleted seed ids) live under one root so a volume can keep them across deploys:

| Path | Contents |
|---|---|
| `$HD_PERSIST_ROOT/data` | `app.db`, `seed_overrides.json`, `secret_key` |
| `$HD_PERSIST_ROOT/uploads` | Seller covers and seed-override images |

Leave `HD_PERSIST_ROOT` unset for local/dev. The app then uses the repo root, same as before, and creates `data/` and `uploads/` on boot.

Production: set `HD_PERSIST_ROOT=/data` and mount a Railway volume at `/data`. Both directories are created on boot under that mount. Without the volume, Railway's disk is wiped on every deploy and uploaded covers fall back to the repo seed images. Inbox JSON stays in `inbox/` next to the app (short-lived drain buffer only).

## Manual check

- `/register` then `/login`
- `/post` to create a deal
- `/deals` to see only your deals
- Editing someone else’s `/deals/<id>/edit` returns 403
