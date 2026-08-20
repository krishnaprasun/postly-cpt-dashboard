# Postly Performance

Live Meta spend × Branch trials, with CPT at every level: combined, ad account,
campaign, active ad set, and ad. Covers **both** ad accounts (`Postly` and
`Postly Install`). Signups and trial mandates come from the Classplus DB alongside,
at the same levels. Read-only — nothing here writes to Meta.

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
- **Signups & mandates** — from the Classplus Redash query, joined on the same ad name
  and rolled up the same way: Signups, Cost/signup, Mandates, Cost/mandate, D0 active,
  D0 cancel, plus three KPIs. The `signups & mandates` checkbox hides the columns; the
  checkbox only appears when the data is actually available for the window on screen.
  See [Classplus](#classplus-signups--mandates) for what a mandate counts and why it
  will not match the Branch trial figure exactly.
- **Range** — a dropdown, defaulting to **Today**: Today, Yesterday, Last 3 days,
  Last 7 days, Custom range. Custom reveals two date pickers seeded to the last 7 days
  and applies on click, not on every keystroke — a half-typed date must not fire a pull.
  Both pickers are capped at today IST. Spans over **31 days** are refused with the
  reason: Branch is fetched in 7-day chunks, so a quarter is dozens of round trips plus
  a full Meta pull per account.
- **Trial event** — CPT is always `postly_trial_started_backend`: the daily report's
  definition and what the ₹150 target is set against. A UI toggle for
  `postly_trial_nc_after10min_backend` was removed on 2026-08-20; the backend still
  fetches that event (one extra Branch call, `t10m` in the API response), so restoring
  the switch is a template-only change.
- **Auto 30m** — always on, with no way to switch it off. The value of this page is that
  the number on screen is current; someone leaving it on a stale window and acting on it
  is the exact failure it exists to prevent. While the tab is open
  it also pings `/healthz` every 10 minutes, because the free instance sleeps after ~15
  minutes idle and a bare 30-minute cycle would pay a cold wake every single time. The
  pings stop with the tab, so nothing is kept awake when nobody is looking. Manual
  `Refresh` forces a pull.
- **Loading** — a foreground pull blurs the page behind a card naming the three sources
  being read and counting the seconds elapsed. A cold load lays skeleton KPI tiles and
  table rows down first, so the blur has the real shape of the page behind it rather than
  an empty screen. It is raised for foreground actions **only** — first load, a range
  change, a manual Refresh. The 30-minute auto refresh, the tab-focus re-pull and the
  follow-up after a stale response stay silent behind the thin progress bar: blurring a
  table someone is reading, every half hour, would be worse than a slightly stale one.
- **Freshness** — the header always states when the figures were pulled
  ("Last refreshed 16:06:57 IST · 3 min ago · auto 30m"), turning amber past 35 minutes. Returning to the tab re-pulls anything
  older than two minutes, because coming back to a stale CPT and acting on it is the
  failure mode that matters. Any fetch in flight shows a progress bar, a spinner on the
  timestamp and a "Refreshing…" button — during a background refresh the current numbers
  stay on screen rather than blanking, so the page is never empty once it has data.
  Server-side cache is 90s (`CACHE_TTL`); past that it is served stale and refreshed
  behind the request rather than blocking it.

## Look

Palette is the Postly brand kit, taken from `postly-insta-daily/brandkit.py` rather than
invented: cream ground `#FDFCF7`, navy ink `#1A1C2E`, green `#20A75D`, gold `#E8A017`.
The mark in the header is the real `postly_icon_logo.svg` from the Creative Tool repo,
inlined so it needs no request, and reused as the favicon.

CPT colours reuse the brand rather than adding a second language: green under ₹150, gold
to ₹255, brick red above. **The semantic `.good/.warn/.bad` rules must stay at the bottom
of the stylesheet** — `.kpi .v`, `td` and `tfoot td` each set a colour and out-specify a
bare `.bad`, which silently killed the colour coding the first time this was styled.

## Meta rate limits

The Meta app is on the **`development_access`** ads-API tier. Requesting **Standard
Access** in the Meta app dashboard is the durable fix; everything below is what the
dashboard does so a throttle is survivable meanwhile.

Read the `x-business-use-case-usage` header before theorising — it names the limit:

```
call_count=1%   total_cputime=6%   total_time=108%   estimated_time_to_regain_access=10   tier=development_access
```

The ceiling that bites is **total request time**, not call count. So the thing to minimise
is *expensive listings*, not the number of requests. It is also **not per-edge**: when
`act_…/adsets` is throttled, `{campaign_id}/adsets` is throttled too. What survives is
`/campaigns`, `/insights`, and single-object reads — listing ad sets and ads is what gets
cut off. And do not poll a throttled endpoint waiting for it to clear: blocked attempts
still accrue against the window and hold it open.

- **Two Meta calls per refresh, not eleven.** The roster (campaigns, ad sets, ads —
  names, statuses, budgets) is cached for 30 minutes (`ROSTER_TTL`; the ads listing, the
  priciest and least urgent, gets 60 via `ADS_ROSTER_TTL`), because it changes on the
  timescale of ad-ops decisions, not seconds. Only the ad-level insights call runs every
  refresh. Builds went ~12s to ~5.5s.
- **Each roster listing fails independently.** Fetching the three as a unit meant one
  throttled listing threw away the other two — losing every budget because the *ads*
  listing failed, when the ad set listing had answered fine.
- **Roster listings do not retry a rate limit** (`rl_retries=0`); the caller degrades
  anyway, and waiting 15s to be told "no" again made a throttled cold start take 44s
  instead of 11s. The insights call keeps its retries — it is the one thing with no
  fallback.
- **A failed roster is cached too** (`ROSTER_RETRY`, 300s). Re-asking a throttled endpoint
  every refresh both feeds the limit and costs the full back-off on each build.
- **Rate limits fail fast.** Two short retries, then give up — Meta holds these for
  minutes, longer than any request should wait, and hammering makes it worse.
- **The retry waits exactly as long as Meta says**, not a generic back-off:
  `estimated_time_to_regain_access` off the throttled response sets `_roster_fail_until`.
  Asking early does not work and blocked attempts still count against the window, so an
  eager loop keeps the throttle alive — a 60-second poller once held one open for half an
  hour.
- **The page says so, in plain words, with a deadline.** `rate_limit_report()` returns
  which accounts are throttled, which listings (`campaigns` / `ad sets` / `ads` /
  `spend`), how long it has been going on and when Meta said it ends. The banner counts
  down and re-pulls itself at zero — silently, so it never blurs a table mid-read.
  Nothing in that message is invented: an earlier version promised a throttle "usually
  clears within a few minutes" and it then ran unbroken for over half an hour, so either
  Meta's own figure is quoted or no time is given.
- **The banner is honest about what is still true.** If only the roster listings are
  throttled it says spend, trials and CPT are current and correct, because insights are
  not affected. If the *spend* read is the throttled one it says the opposite — every
  figure on screen is the last one that came through. Getting that backwards is exactly
  the kind of false reassurance that makes someone act on a stale CPT.
- **Throttle state is never cached.** A cached payload is re-stamped with a live
  `rate_limit` block on the way out (`_with_live_limits`). Serving the cached one would
  count down to a deadline that had already passed, or claim all-clear while a refresh is
  being refused right now.
- **The budget is reported on the way up, not only after it trips.** Every response
  carries the usage header, so `worst_time_pct` is always known; past 85% the page warns
  that refreshes may start being refused. `total_time` is the number that matters —
  watching call count would have missed every throttle so far.
- **A throttle never blanks the dashboard.** Two fallbacks, in order:
  1. If a build fails and any figures were previously fetched for that window, those are
     served with an amber "not refreshing" banner and their age.
  2. If there is nothing cached, the build **degrades to insights-only**. The insights
     call alone carries every id and name needed for spend, trials and CPT at every
     level — only statuses and budgets are lost. The affected account is listed in
     `degraded`, the UI says so, and the budget and "live ad sets" KPIs stop claiming
     numbers they cannot know rather than showing a wrong one.

Only a cold cache *and* a failing insights call is a hard error.

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

## Classplus (signups & mandates)

Redash query `19634` on `data.classplus.co`, read through its query API key.

**Cost/mandate is a second opinion on CPT.** It is the same rupees-per-trial unit,
built from the product database instead of the attribution SDK, and it is coloured
against the same ₹150 target. On 2026-08-20 the two agreed to within 3% combined and
to within one or two per ad on the top spenders — which is the point of having it.

Three things about this source shape the code:

- **The window is written into the SQL.** The query takes no parameters; its bounds are
  literal IST dates. `_cp_window()` reads them back out of the SQL text and the columns
  are attached **only** when they match the window on screen — otherwise the page says
  which day the query covers and shows nothing. Putting one day's mandates next to
  another day's spend would be worse than a blank column. The practical consequence:
  the query's dates have to be moved forward for "today" to keep working, or the query
  parameterised with `{{start}}`/`{{end}}` so the dashboard can pass a range.
- **It is a signup-cohort measure.** `trial_mandates` counts trials taken by people who
  *signed up inside the window*. Branch counts trial events inside the window whoever
  they came from. Someone who signed up yesterday and started a trial today counts for
  Branch and not here. That is why the two columns differ slightly, and why they stay
  separate columns rather than being reconciled into one.
- **It is optional.** No key, or a query that fails, and the dashboard behaves exactly
  as it did before. Meta and Branch are what CPT rests on; a third source must never be
  able to block them. Failures are cached the same way roster failures are.

Freshness is negotiated with Redash rather than polled: the fetch POSTs `max_age`
(`CP_TTL`, 600s), so Redash either returns its cached result or starts a run. A run is
polled for at most `CP_POLL_BUDGET` (20s) — the query takes ~13s and a cold page is not
going to wait — and whatever happens the last result is served with its age shown. The
numbers are then read from `results.json`, not from the POST response, because only
`results.json` carries the SQL, and the SQL is the only place the window is recorded.

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

1. env `META_TOKEN`, `BRANCH_KEY`, `BRANCH_SECRET`,
   `CLASSPLUS_API_KEY` / `CLASSPLUS_QUERY_ID` / `CLASSPLUS_HOST` — this is what Render uses
2. `~/.anthropic/meta_token`, `~/.anthropic/branch_creds.json`,
   `~/.anthropic/classplus_creds.json` — local runs
3. `~/Desktop/Postly Ads Management/postly_config.py` — last-resort local fallback

The Classplus trio is the one set that is allowed to be absent: missing it turns the
signup and mandate columns off and changes nothing else.

It cannot rely on (3) alone: macOS protects `~/Desktop` and a process launched as a
server is not granted access to it, so that import fails at serve time even though it
works from a shell. If a token is rotated, update `~/.anthropic/` **and** the Render
env var.
