# Postly — Live CPT dashboard

Live Meta spend × Branch trials, with CPT at every level: combined, ad account,
campaign, active ad set, and ad. Covers **both** ad accounts (`Postly` and
`Postly Install`). Read-only — nothing here writes to Meta.

## Run locally

```bash
~/postly-cpt-dashboard/run.sh
```

Then open <http://127.0.0.1:8787>. `PORT=9000 ./run.sh` to move it.
Credentials come from `~/.anthropic/` (see [Credentials](#credentials)).

## What it shows

- **Current CPT** — combined spend / trials, coloured against the ₹150 target
  (green ≤ ₹150, amber to ₹255, red above).
- **Tabs** — Combined (account → campaign tree), Ad accounts, Campaigns, Ad sets, Ads.
  Every column sorts; the name filter searches name, parent ad set and campaign.
  "active only" is on by default; objects that spent in the window but are now paused
  appear when you turn it off, tagged `paused`.
- **Ranges** — Today / Yesterday / 3d / 7d (Branch caps a request at 7 days).
- **Event toggle** — CPT can be measured against either trial event:
  - `Trial 101` = `postly_trial_started_backend` — the daily report's CPT definition
    and what the ₹150 target is set against. **This is the default.**
  - `Trial 10min` = `postly_trial_nc_after10min_backend` — the event the
    Trial_10min campaigns actually optimise for.
- **Auto 5m** — re-pulls every five minutes. Manual `Refresh` forces a pull.

## How CPT is computed

`CPT = Meta spend / Branch trials`, joined at **ad-name** level and then rolled up
ad → ad set → campaign → account → combined.

The ad-name join is not an arbitrary choice. Branch only exposes `~ad_name` and
`~ad_set_name` as attribution dimensions — `~campaign_name` comes back empty, so
campaign and account totals cannot be read from Branch directly. Ad-set names also
merge across the Testing and Trial campaigns, which corrupts an ad-set-name join.
Joining once at ad level and summing upward avoids both problems and keeps every
level internally consistent.

Verified 2026-08-20 against Branch's own `~ad_set_name` dimension: of 164 ad sets,
163 matched within 5 trials.

Both ad accounts are set to Asia/Kolkata, so Meta's day boundary and Branch's
IST day boundary are the same — no timezone skew in the join.

### Caveats the UI states on every view

- **Attribution coverage.** Branch trials whose ad name matches no ad in either
  account — organic, other channels, deleted ads — sit outside every row. The
  Attribution KPI shows what fraction is covered (~95% on a normal day).
- **Shared ad names.** Where several ads share one name, Branch cannot tell them
  apart. Their trials are split by spend share, so rollups stay exact while the
  individual per-ad CPTs are an even-CPT assumption. Those rows are tagged
  `shared name`.
- **Intraday.** Meta spend lags a few minutes and Branch trials keep landing
  through the day, so today's CPT reads high in the morning and settles.

## Access

**There is no login.** Anyone with the URL sees today's spend, CPT, budgets, and
every ad set and ad name across both accounts. `.onrender.com` hostnames are public
and guessable, so treat the URL itself as the only control. The app sends
`X-Robots-Tag: noindex, nofollow, noarchive` and a disallow-all `robots.txt` to keep
it out of search results, which stops crawlers but not anyone who has the link.

To turn on a password later, set `ADMIN_PASS` (and optionally `ADMIN_USER`, default
`postly`) in the Render environment — the code path is already there and switches on
by itself when the variable is present. No redeploy needed beyond the restart Render
does when you save an env var.

## Deploy (Render)

Docker → Render, configured by `render.yaml`. Plan is **free**, so the instance sleeps
after ~15 minutes idle. That is handled rather than ignored: the cache is
stale-while-revalidate, so a woken instance answers immediately with its last numbers
and refreshes behind the request, and the page shows a "pulling live data" state on a
genuinely cold start instead of a blank screen.

One-time setup:

1. Render → **New → Web Service** → connect `github.com/krishnaprasun/postly-cpt-dashboard`.
   Connect the **GitHub repo**, not the "public Git URL" option — the public-URL path is
   what left `postly-insta-daily` with no webhook and no auto-deploy on push.
2. Render reads `render.yaml`. Set the three secrets when prompted (they are `sync: false`,
   so they are never in git): `META_TOKEN`, `BRANCH_KEY`, `BRANCH_SECRET`.
3. Check `/healthz` returns `{"ok": true}` before trusting a reading.

After that, **deploy = `git push origin main`** — *if* Render is connected through its
GitHub App. This service was created through the Render REST API instead, so the repo has
no Render webhook (`gh api repos/krishnaprasun/postly-cpt-dashboard/hooks` returns `[]`) and
a push may not trigger anything. Until that is reconnected in the dashboard, deploy with:

```bash
./deploy.sh
```

Free-plan facts worth knowing:

- No persistent disk, and none is needed — the only state is an in-memory cache that
  is meant to be thrown away.
- Health checks do not keep a free instance awake; only real traffic does.
- One gunicorn worker on purpose. The cache is in-process, so a second worker would
  keep its own copy and double the Meta/Branch calls while halving the hit rate.
  `--timeout 180` because a 7-day pull takes ~30s locally and longer on shared CPU;
  gunicorn's 30s default would kill it mid-pull.

## Files

| file | role |
|---|---|
| `postly_cpt.py` | data layer — Meta + Branch pulls, the join, the rollup |
| `server.py` | Flask app: `/`, `/api/data`, `/healthz`, `/robots.txt`, cache |
| `templates/index.html` | the whole UI, no build step, no dependencies |
| `config.py` | ids + credential resolution |
| `Dockerfile`, `render.yaml` | deploy |

## Credentials

Nothing secret is committed. `config.py` reads, first hit wins:

1. env `META_TOKEN`, `BRANCH_KEY`, `BRANCH_SECRET` — this is what Render uses
2. `~/.anthropic/meta_token` and `~/.anthropic/branch_creds.json` — local runs
3. `~/Desktop/Postly Ads Management/postly_config.py` — last-resort local fallback

It cannot rely on (3) alone: macOS protects `~/Desktop` and a process launched as a
server is not granted access to it, so that import fails at serve time even though it
works from a shell. If a token is rotated, update `~/.anthropic/` **and** the Render
env var.
