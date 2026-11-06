# Deploy Harrison Deals submit on Render

This folder is a self-contained Flask app. Local Windows (`serve.bat`) still works. For a public site that does **not** need James's PC online, deploy here as a Render free Python web service.

## Important: ephemeral disk

Render free web services have an **ephemeral filesystem**. That means:

- SQLite at `data/app.db` (users + local deal copies) **resets on redeploy / restart**
- Uploaded covers under `uploads/` can disappear on redeploy
- Inbox JSON under `inbox/` is only a short-lived buffer

**Source of truth for the public landing page is Notion**, not this SQLite. After each post/edit/delete the app writes an inbox event; a routine should drain those events into Notion via HTTP (see below). Do not treat Render SQLite as permanent storage.

## Build / start

| | |
|---|---|
| **buildCommand** | `pip install -r requirements.txt` |
| **startCommand** | `gunicorn -b 0.0.0.0:$PORT app:app` |

`Procfile` and `render.yaml` use the same start command. Runtime hint: `runtime.txt` → `python-3.12.7`.

## Environment variables

Set these in the Render dashboard (or Blueprint):

| Key | Required | Notes |
|---|---|---|
| `SECRET_KEY` | **yes** | Session signing. Generate random hex, e.g. `python -c "import secrets; print(secrets.token_hex(32))"`. Blueprint may use `generateValue`. |
| `HD_INBOX_KEY` | **yes** for drain | Shared secret for `/api/inbox/*`. If unset, inbox endpoints return **503**. |
| `HD_PUBLIC_BASE` | **yes** after first deploy | Public URL with no trailing slash, e.g. `https://harrison-deals-submit.onrender.com`. Needed so cover images get absolute URLs. |
| `HD_LANDING_URL` | optional | Defaults to the Harrison Deals canvas. |
| `PORT` | set by Render | App already reads `PORT`, then falls back to `HD_SUBMIT_PORT` (8788). |

Do **not** commit secrets. `.gitignore` excludes `.env`, `data/`, `uploads/`, `inbox/*.json`.

## Inbox HTTP drain (no Windows PC)

When the app runs on Render, the Notion intake routine should pull over HTTP instead of reading a local folder:

```http
GET /api/inbox/pending?consume=1
X-HD-Inbox-Key: <HD_INBOX_KEY>
```

- Returns `{ ok, count, consumed, events: [...] }` and **deletes** the JSON files (best-effort) when `consume=1`.
- Without `consume=1`, events are returned but files stay. Then ack after Notion write:

```http
POST /api/inbox/ack
X-HD-Inbox-Key: <HD_INBOX_KEY>
Content-Type: application/json

{"ids": ["event-uuid-1", "event-uuid-2"]}
```

Key may also be passed as `?key=` (prefer header).

## Blueprint (optional)

`render.yaml` defines service `harrison-deals-submit` on the free plan. `HD_PUBLIC_BASE` is `sync: false` — set it manually to the service URL after the first deploy. If Blueprint `generateValue` is rejected by your deploy path, create `SECRET_KEY` and `HD_INBOX_KEY` as secret env vars in the dashboard instead.

## After deploy checklist

1. Copy the Render URL → set `HD_PUBLIC_BASE`.
2. Confirm `SECRET_KEY` and `HD_INBOX_KEY` are set.
3. Point the landing page **Post Deal** button at `https://<service>/post`.
4. Point the web-intake routine at `GET /api/inbox/pending?consume=1` with the inbox key.
5. Smoke-test: register → post a deal → drain inbox → Notion row appears.

## Local Windows (unchanged)

`serve.bat` / `serve.sh` still run `python app.py` on `HD_SUBMIT_PORT` (default 8788). Cloudflare tunnel remains optional for local-only testing.
