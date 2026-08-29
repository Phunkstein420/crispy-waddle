# crispy-waddle

CRE Public Notice Signals — public-record facts for commercial real estate
distress, built for Douglas Magnuson.

## What this API returns

Three honest signal families only:

| Family | Event types collected | Sources |
| --- | --- | --- |
| `business_bankruptcy` | `bk_petition_rss` | CM/ECF public RSS XML for `deb`, `nysb`, `nyeb`, `txsb`, `cacb`, `ilnb`, `flsb` |
| `business_bankruptcy` | `bk_8k` | SEC EDGAR full-text search for 8-K **Item 1.03** (Bankruptcy or Receivership) |
| `occupancy_distress` | `occupancy_execution` | NYC Open Data commercial marshal executions |

Each event is **facts + `source_url` + `collected_at`**. No SSNs, no newspaper body HTML, no PACER docket PDFs.

## What this does not do

- Does not log into PACER or fetch `doc1` / `DktRpt.pl` / PDF documents. CM/ECF collection is **RSS XML only** (`https://ecf.{code}.uscourts.gov/cgi-bin/rss_outside.pl`).
- Does not scrape or market lease-default letters.
- Does not scrape FL / TX / GA press portals (`floridapublicnotices.com`, `texaspublicnotices.com`, `georgiapublicnotice.com`). Those rows stay in `GET /v1/sources` as **catalog-only** (`license_required`).
- Does not scrape NYS WebCivil Local (Cloudflare / no sanctioned bulk path).

## HTTP API

Deployed as a Python FastAPI service. OpenAPI: `/openapi.json` (Swagger UI at `/docs`).

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/health` | Render health check |
| GET | `/v1/sources` | Source catalog, including license-gated portals |
| GET | `/v1/events` | Query params: `signal_family`, `event_type`, `jurisdiction_state`, `occupancy_class`, `limit`, `offset` |
| GET | `/v1/events/{id}` | Single event |

If `RAPIDAPI_PROXY_SECRET` is set, protected routes require a matching `X-RapidAPI-Proxy-Secret` header. RapidAPI listing is **not** published by this repo.

## Collector

`bot_engine.py` runs as a background loop inside the web process (and as `python bot_engine.py` for a one-shot/cron run). It only collects sources with `collection_status = approved` and `license_status = none`. Rate limits and `robots.txt` are respected. Northern District of Illinois (`ecf.ilnb.uscourts.gov`) currently publishes `Disallow: /`, so that RSS host is skipped. Bankruptcy RSS items are kept only when they are **Type: bk**, look like a **petition**, and the debtor name looks like a **business entity**.

## Configuration (env vars, never commit secrets)

See `.env.example`.

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Postgres URI for the existing Supabase project (`iuuuvmvqxsugrbfnfgxk`) |
| `CONTACT_EMAIL` | Required by SEC.gov User-Agent policy |
| `RAPIDAPI_PROXY_SECRET` | Optional; RapidAPI proxy header |
| `COLLECTOR_ENABLED` | `true`/`false` |
| `COLLECTOR_INTERVAL_SECONDS` | Default 21600 (6 hours) |
| `PORT` | Bind `0.0.0.0:$PORT` (Render sets this) |

## Local

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # fill DATABASE_URL
pytest
uvicorn main:app --host 0.0.0.0 --port 8000
```

Supabase tables `public.sources` and `public.events` already exist; this app does not recreate the project.

## Deploy on Render

`render.yaml` defines a Python web service that binds `0.0.0.0:$PORT`. Create it from this repo in workspace `tea-da995d142hec73f5dtmg` (My Workspace):

https://dashboard.render.com/blueprint/new?repo=https://github.com/Phunkstein420/crispy-waddle

Set these env vars in the Dashboard (do not commit them):

- `DATABASE_URL` — Supabase pooler URI for the app role
- `CONTACT_EMAIL` — used in the SEC.gov User-Agent
- `RAPIDAPI_PROXY_SECRET` — optional; when set, `/v1/*` requires `X-RapidAPI-Proxy-Secret`

This project does not publish a RapidAPI listing.
