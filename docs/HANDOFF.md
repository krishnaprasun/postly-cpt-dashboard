# Ads Performance — handoff

**What it is.** One Flask app that answers *what is a trial costing us right now*, for
three brands, across two ad platforms, at every level from the whole account down to the
individual creative. Read-only: it never writes to Meta, Google or Branch.

**Live:** https://postly-cpt-dashboard.onrender.com
**Repo:** https://github.com/krishnaprasun/postly-cpt-dashboard — **PUBLIC**
**Local:** `~/postly-cpt-dashboard`, `./run.sh` → http://127.0.0.1:8787

> The repo is public. Nothing secret may ever be committed. Run a secret scan before
> every push. Every credential is an env var on Render or a file under `~/.anthropic/`.

Read this document first, then `docs/RUNBOOK.md` for operations and `docs/DECISIONS.md`
for the standing decisions you must not silently reverse. `README.md` at the repo root is
the reference manual — 1,400 lines, every mechanism explained; this is the map to it.

---

## 1. The system in one page

```
  Meta Marketing API v21.0 ─┐
                            ├─→  postly_cpt.py  ─→  server.py (Flask)  ─→  templates/index.html
  Branch Query API ─────────┤         │                   │
                            │         │                   ├─→ GCS history store (settled days)
  Google Ads API v22 ───────┘         │                   └─→ browser IndexedDB cache
  Classplus Redash (Postly only) ─────┘
```

**Meta** gives spend, impressions, clicks, budgets and statuses. **Branch** gives trials
and installs, attributed to an ad by name. **Google Ads** gives spend per campaign per ad
group. **Classplus** (Postly only, optional) gives signups and trial mandates.

Three things are worth understanding before you change anything:

**The join is on names, not ids.** Branch exposes `~ad_name` and `~ad_set_name` but no
ids, so ad name is the only shared key with Meta. For Google, Branch leaves the ad name
empty but *fills in campaign and ad group* — which is why a Google CPT exists at all, and
why it stops at ad-group level. A renamed campaign shows as two rows until the window
rolls past the rename.

**Pro rata is the only reading.** Branch trials that carry no ad name are allocated to
Meta and Google in proportion to each platform's share of *attributed* volume. Google's
own measured trials are never in the pool being divided — that was a real bug, and putting
them back makes the calculation circular. `meta_pro + google_pro == branch_total`, exactly.
There is no measured/pro-rata switch; the numbers on the page are always pro rata.

**A missing value is not a zero.** A stored day lacking an impressions key means *we did
not record it*, not *there were no impressions*. Trials Branch refused to answer means
*unknown*, not *none*. Every one of these renders as a dash. Two separate live bugs came
from collapsing that distinction — one of them showed a blended CPT of ₹354 when the true
figure was ₹191.

---

## 2. Brands

Everything brand-specific lives in one table: `BRANDS` in `config.py`.

| brand | Meta accounts | Branch headline event | CPT target | Classplus |
|---|---|---|---:|---|
| Postly | `act_964790132585820`, `act_2383113182218548` | `postly_trial_started_backend` | ₹150 | yes |
| Speakeasy | `act_874500498817876`, `act_909676394829541` | `speakeasy_trial_started` | ₹275 | no |
| Funda | `act_1415034359774559`, `act_1662727118397158`, `act_826851770432701` | `trial_started_backend` | ₹180 | no |

One Meta token reaches all seven accounts. Branch is one app per brand, so one key/secret
pair each. Google Ads is one OAuth credential reaching 18 customer accounts through the
Testbook MCC.

**Ad-name match rates differ enormously by brand and must never be read as a data bug:**
of each brand's *attributed* Branch trials, the share whose ad name exists in Meta is
Funda 99.7%, Postly ~95%, Speakeasy 62%.

Adding a fourth brand is: one entry in `BRANDS`, one Branch key pair, one `BRAND_LINK_*`
env var. A brand with no Branch key is a supported state — its Meta side works in full and
the trial and CPT columns are simply hidden.

---

## 3. Where everything lives

### Hosting

| | |
|---|---|
| Render service | `srv-da3cttibkg8c738a4nvg`, workspace `tea-d9tnoaqjobas73df4bpg` |
| Region / plan | Singapore, **free** |
| Runtime | Docker, gunicorn, **1 worker / 8 threads / 180s timeout** |
| Deploy | `git push origin main` — autoDeploy is on |
| Health check | `/healthz` (public, cheap, never triggers an upstream pull) |

`render.yaml` is **documentation only** for this service. It was created through the REST
API, not the Blueprint flow, so plan, region, health check and env vars live in Render's
own config. Editing `render.yaml` changes nothing.

### The history store

Settled days are served from GCS rather than re-pulled on every view. A 30-day window is
27 stored + 3 live; today is entirely live.

| | |
|---|---|
| Bucket | `admanagementpostly-ads-history` (asia-south1, public access prevented) |
| Front door | Cloud Run `ads-history` → `https://ads-history-360124450287.asia-south1.run.app` |
| Service account | `ads-history@admanagementpostly.iam.gserviceaccount.com`, `storage.objectAdmin` on that bucket only |
| Auth | bearer token — `~/.anthropic/ads_history_token`, Render `HISTORY_TOKEN` |

**Why a proxy and not direct GCS:** the org enforces
`constraints/iam.disableServiceAccountKeyCreation`, so no downloadable SA key exists, and
Render issues no OIDC identity Google accepts. No Google credential can live on Render.
The Cloud Run service uses ambient credentials and enforces the bearer token itself with
`hmac.compare_digest`; it is `--allow-unauthenticated` at the IAM layer on purpose.

`/healthz` on **that** service returns a Google-branded 404, intercepted at Google's edge.
Not a bug. Use `/v1/have` as its liveness probe.

### Scheduled jobs

21 `ads-*` jobs in Cloud Scheduler, `admanagementpostly` / asia-south1, all Asia/Kolkata.
They authorize with `Authorization: Bearer <HISTORY_TOKEN>`.

| jobs | schedule | what |
|---|---|---|
| `ads-history-{postly,speakeasy,funda}` | 03:40 / 03:50 / 04:00 | store settled days |
| `ads-longevity-{3 brands}` | 04:15–04:35 and 13:15–13:35 | precompute the Lifespan tab |
| `ads-budget-{3 brands}-{9,15,23}` | 09:xx, 15:xx, 23:xx | budget snapshots (forward-only) |
| `ads-reach-backfill-{3 brands}` | every 10 min, 00–02 | **complete — delete these** |
| `ads-google-backfill-{3 brands}` | every 20 min, 01–04 | Postly has 14 days left; then delete |

### Credentials

Every one of these is either an env var on Render or a `chmod 600` file under
`~/.anthropic/`. `config.py` resolves env → `~/.anthropic/` → the Desktop toolkit, in
that order.

| file | env var(s) | reaches |
|---|---|---|
| `meta_token` | `META_TOKEN` | all 7 Meta ad accounts (system user, non-expiring) |
| `branch_creds.json` | `BRANCH_KEY/SECRET`, `SPEAKEASY_BRANCH_*`, `FUNDA_BRANCH_*` | one Branch app per brand |
| `google_ads.json` | `GOOGLE_ADS_{CLIENT_ID,CLIENT_SECRET,REFRESH_TOKEN,DEVELOPER_TOKEN,LOGIN_CUSTOMER_ID}` | 18 Google customers via MCC `3343252288` |
| `classplus_creds.json` | `CLASSPLUS_QUERIES`, `CLASSPLUS_HOST` | Redash query 19695 (Postly only) |
| `ads_history_token` | `HISTORY_TOKEN`, `HISTORY_URL` | the GCS store + every ops endpoint |
| `brand_links.json` | `BRAND_LINK_{POSTLY,SPEAKEASY,FUNDA,ALL}` | per-team access links |
| `render_key` | — | the Render API |

**macOS gotcha worth remembering beyond this project:** a process launched as a dev server
(and likely launchd/cron too) is not granted access to `~/Desktop`, so importing from
`~/Desktop/Postly Ads Management/` raises `ModuleNotFoundError` at serve time even though
the identical import works from a shell. Nothing long-running may depend on a path under
`~/Desktop`, `~/Documents` or `~/Downloads`.

---

## 4. Access

There is **no login**, by the owner's explicit decision after being told a public
`.onrender.com` URL exposes live spend, budgets and every ad name. Mitigations shipped
instead: `X-Robots-Tag: noindex`, a disallow-all `robots.txt`, and per-brand links.

**Per-brand links** narrow "anyone with the URL" from one secret covering everything to
one secret per team. `https://postly-cpt-dashboard.onrender.com/b/<value>`. Opening one
locks the page to that brand and hides the brand switcher; every API call carries `k=` and
the server narrows the request. A valid key asking for another brand is served *its own*
brand; an invalid key gets a 403 naming no brands and revealing no count.

**The link is not a login.** Do not describe it as one. A forwarded link works until
rotated, and rotating is changing one env var — no deploy.

`ROOT_OPEN` is currently **`1`**, so the bare `/` URL still serves all brands with full
controls. Set it to `0` the day every team is holding its own link.

A `full` capability gates the two things that make the app *spend*: a forced Meta roster
re-read and a longevity recompute. Team links are read-only in that sense; `BRAND_LINK_ALL`
is not. Ops endpoints (`/api/backfill/*`, `/api/snapshot`, `/api/budgets/snapshot`,
`/api/precompute`) ignore links entirely and require the bearer token.

---

## 5. Files

| file | lines | what |
|---|---:|---|
| `postly_cpt.py` | 3,561 | every pull, the join, the rollup, pro rata, caching, backfills |
| `templates/index.html` | 3,839 | the whole UI — no build step, no framework, no bundler |
| `server.py` | 949 | Flask routes, the payload cache, gzip, the ops endpoints |
| `config.py` | 324 | brands, credentials resolution, brand links |
| `google_ads.py` | 296 | OAuth refresh, customer discovery, GAQL spend per ad group |
| `history.py` | 283 | the GCS store client |
| `tools/*.py` | 832 | backfills and the Google credential tools |
| `README.md` | 1,400+ | the reference manual |

**There is no build step.** `index.html` is served as-is. That is deliberate: it keeps the
whole UI editable by anyone who can read HTML, and it means a deploy is a `git push`.

### API surface

`/api/data` (the main payload) · `/api/series` (per-day) · `/api/prior` (previous period)
· `/api/longevity` · `/api/google`, `/api/google/{series,spend,status}` · `/api/budgets`,
`/api/budgets/snapshot` · `/api/backfill/{reach,google}` · `/api/snapshot` ·
`/api/precompute` · `/api/preview` (302 to the Meta creative, `Referrer-Policy: no-referrer`)
· `/healthz` · `/robots.txt`

---

## 6. Caching — three layers, and why

Meta and Branch are the constraint. Everything here exists to reduce calls to them.

1. **In-process payload cache**, `CACHE_TTL=780s`, stale-while-revalidate. A woken
   instance answers instantly from its last numbers and refreshes behind the request.
   **One gunicorn worker on purpose** — this cache is in-process, so a second worker keeps
   its own copy, doubling upstream calls while halving the hit rate.
2. **GCS-persisted artifacts** — settled days, and the payload cache itself, so a cold
   instance serves the last numbers in ~0.5s instead of paying a 10–17s rebuild on top of
   a 15–30s wake.
3. **Browser IndexedDB**, soft 90s / TTL 15 min / hard 12 h, keyed on `APP_VERSION`
   (derived from file mtimes). A warm reload renders the page in 13ms and 4,136 matrix
   rows in 80ms with zero network.

The page refreshes every **15 minutes, unforced** — the first tab past the server TTL
triggers one rebuild and every other tab and person is served from it. Hidden tabs skip
the pull entirely. A `/healthz` ping every 10 minutes keeps the instance warm and touches
no upstream API.

**Version stamps invalidate stale artifacts:** `PRORATA_MODEL`, `SERIES_SHAPE`, `row_cap`,
`APP_VERSION`. Change the shape of a stored thing, bump its stamp — otherwise old
artifacts are served against new code, which has already happened twice.

**Counter-intuitive:** a 3-day window is entirely inside the settle window, so it is 100%
live and is the *slowest* view. 30- and 90-day windows are mostly stored and are faster.

---

## 7. Current state, verified 2026-08-27

- Google Ads is **fully live** — spend, impressions and clicks per campaign per ad group.
  Last 7 days: Postly ₹1.59 L, SpeakEasy ₹14.2 L, Funda ₹1.99 Cr.
- Reach backfill (impressions/clicks into stored days): **complete, all three brands.**
- Google trials backfill: Speakeasy and Funda complete (97 days each); **Postly has 14
  days left**, which its 01–04 IST schedule will finish on its own.
- 21 scheduled jobs enabled, nothing failing.
- `/healthz` 200 in 0.18s.

### Open items — owner action required

| item | why it matters | urgency |
|---|---|---|
| **Publish the Google OAuth consent screen** in project `734843757980` | a refresh token minted while the screen is in *Testing* dies after 7 days — this already happened once and took Google spend off the page | **before ~2026-09-02** |
| **Rotate the Render API key** | it was pasted into a chat transcript against advice; a Render key grants access to every workspace the account belongs to and cannot be scoped down | soon |
| Delete `ads-reach-backfill-*` (3 jobs) | complete; they wake the free instance 3 h/night against a 750 h monthly pool | now |
| Delete `ads-google-backfill-*` (3 jobs) | after Postly's last 14 days land tonight | tomorrow |
| Rotate `HISTORY_TOKEN` | it was echoed into a transcript by a gcloud error message; carried by 9+ scheduler jobs, so rotation means updating each | optional |
| Request Meta Standard Access | raises the rate limits that currently force degraded budget/status reads | optional |
| Set `ROOT_OPEN=0` | closes the bare URL once every team holds its own link | when handover is done |
