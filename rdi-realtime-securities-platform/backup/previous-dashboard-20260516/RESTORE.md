# Dashboard snapshot — 2026-05-16

This is a verbatim copy of the `dashboard/` folder as it was on
**2026-05-16, 20:11 IST**, just before the dashboard was reimagined
for the Securities & Trading Firm architects' demo.

## What this snapshot contains

The customer-facing portfolio dashboard:

- `Dockerfile` · Python 3.12-slim image
- `app.py` · 8 Flask routes
  - `/`
  - `/api/customers` · `/api/customer-search`
  - `/api/scale-benchmark`
  - `/api/portfolio/<client_code>` · `/api/latency/<client_code>`
  - `/api/pipeline`
  - `/api/recent-trades/<client_code>`
- `templates/index.html` · single-page customer-portfolio view,
  Redis-brand styled, with KPIs / search / portfolio / scale-bench /
  pipeline-status / recent-trades sections
- `requirements.txt` · `Flask`, `redis`, `psycopg`
- `static/` · empty

## How to restore

If you ever want this old dashboard back:

```bash

# 1. (optional) snapshot whatever is in dashboard/ today, before overwriting
mv dashboard dashboard.replaced-$(date +%Y%m%d-%H%M%S)

# 2. restore from this backup
cp -R backup/previous-dashboard-20260516 dashboard
rm dashboard/RESTORE.md          # this file shouldn't ship with the live image

# 3. rebuild + restart
docker compose up -d --build dashboard
```

The Postgres source data, the target Redis BDB, the RDI processor, and
the mock RDI API are all untouched by this swap — restoring just
brings the UI back.

## Why we took this snapshot

The reimagined dashboard pivots the demo from a **customer-experience
proof** (current target user: a retail trader) to an **RDI capability
showcase + use-case theatre** (new target user: Securities & Trading Firm
architects and the securities application team). The portfolio view
that used to be the whole dashboard is preserved as one tab of the new
6-tab layout, so the "what does the customer ultimately see?" answer
is still there — just no longer the headline.
