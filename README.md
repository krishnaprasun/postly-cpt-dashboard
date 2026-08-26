# Postly Performance

Live Meta spend × Branch trials, with CPT at every level: combined, ad account,
campaign, active ad set, and ad. Signups and trial mandates come from the Classplus DB
alongside, at the same levels. Read-only — nothing here writes to Meta.

## Brands

One deployment serves three brands, switched from the control beside the title. They
share everything that is hard about this — Meta's rate limits, the roster caching, the
ad-name join, the degradation rules — and differ only in the `BRANDS` table in
`config.py`.

**Trials means trial-started on every brand**, by instruction — one definition across
all three so CPT is comparable between them. The no-cancel-after-10-minutes variant is
fetched and shown beside it but never drives CPT.

Each brand carries its own logo and accent colour, set in the same `BRANDS` entry
(`logo`, `theme`). Switching brand swaps the header logo, the loading card, the favicon,
the page title and the accent in one place (`applyBrand()`), so the page can never wear
one brand's colour under another's name.

**The accent is chrome only.** `--accent` / `--accent-dk` / `--accent-lt` drive the
active tab, hovers, focus rings, the hero wash and stripe, and the progress bars.
`--green` / `--gold` / `--red` are the *semantic* good/warn/bad colours and are never
themed: a good CPT stays green on a gold-themed Speakeasy and a violet-themed Funda,
because the one colour anyone acts on has to mean one thing everywhere.

| brand | accent | ad accounts | Branch events (headline / second) | CPT target | Classplus |
|---|---|---|---|---|---|
| **Postly** | green `#20A75D` | `Postly`, `Postly Install` | `postly_trial_started_backend` / `postly_trial_nc_after10min_backend` | ₹150 | yes |
| **Speakeasy** | gold `#F5B301` | `SpeakEasy`, `SpeakEasy Install` | `speakeasy_trial_started` / `SE_trial_nc_after_10mins` | none | no |
| **Funda** | violet `#6A4BD8` | `Funda`, `Funda Earning App`, `Funda 3` | `trial_started_backend` / `trial_nc_after10min_backend` | none | no |

Two states are deliberate rather than unfinished:

- **No CPT target** (`cpt_target: None`) shows the cost-per figures uncoloured. A target
  nobody has agreed on is worse than none, because a red cell reads as an instruction.
- **No Branch app** drops the trial and CPT columns entirely rather than filling them
  with zeros — a zero is a claim that nothing happened, which is not what a missing key
  means. The brand still gets its full Meta side: spend, budgets, statuses, at every
  level. Supplying the key and naming two events in `BRANDS` is the whole change needed
  to light the columns up; all three brands are wired as of 2026-08-22, but the state is
  kept because it is what any fourth brand starts in.

Ad-name match rates, measured 2026-08-21 — the share of each brand's *attributed* Branch
trials whose ad name exists in Meta. Low is not a bug, it is how much of the funnel that
brand runs outside these ad accounts, and the Attribution KPI states it on every page:

| brand | attributed trials matched to a Meta ad |
|---|---|
| Funda | 99.7% |
| Postly | ~95% |
| Speakeasy | 62% |

The brand is part of the cache key, not a filter applied afterwards: each brand is its
own set of Meta and Branch calls, so one brand's throttle can never evict or stale
another's figures.

## Run locally

```bash
~/postly-cpt-dashboard/run.sh
```

Then open <http://127.0.0.1:8787>. `PORT=9000 ./run.sh` to move it.
Credentials come from `~/.anthropic/` (see [Credentials](#credentials)).

## What it shows

- **Current CPT** — combined spend / trials, coloured against the brand's target where
  it has one (Postly: green ≤ ₹150, amber to ₹255, red above; the other brands show it
  uncoloured). A brand with no Branch app leads with Spend instead, because a CPT tile
  that could only ever read "—" would make a missing key look like zero trials.
- **Tabs** — Combined (account → campaign tree), Ad accounts, Campaigns, Ad sets, Ads.
  The parent-name column (campaign under ad set, account under campaign, ad set under ad)
  is left-aligned header *and* cell, and takes the row's spare width so it sits beside the
  name it belongs to. Auto table layout otherwise gives the slack to the widest column —
  the name — which pushed the parent off to the far right under a right-aligned header.
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
  Last 7 days, Last 30 days, Custom range. Custom reveals two date pickers seeded to the last 7 days
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

## Testing vs trial

The header carries **Trial / Testing / Blended**. They are separated because they buy
different things: a testing campaign is looking for a creative that works and is expected
to cost more per trial; a trial campaign scales one that already does. Averaging them
gives a number true of neither.

Postly, 30 days to 2026-08-24:

| | Spend | Trials | CPT |
|---|---|---|---|
| trial | 92,84,003 | 49,976 | **186** |
| testing | 12,07,976 | 1,515 | **797** |
| blended | 1,04,91,979 | 51,491 | 204 |

The ₹150 target is a *trial* target. Against blended ₹204 trial reads 36% off; it is 24%
off. And a testing ad set judged against ₹150 is killed for doing its job, which is how
the pipeline feeding trial dries up. Funda over 7 days: trial 196, testing 596, blended 202.

**The split is decided once, on the campaign** — name matching `testing_re`, default
`(?i)testing`, per-brand in `config.py` — and inherited by its ad sets and ads. Matching
ad set names instead would be a second source of truth that disagrees with the first,
because an ad set name travels with its creative when it graduates out of testing while
the campaign it sits in is the thing that actually changed. Verified across all seven ad
accounts: every brand names testing campaigns with the word.

Anything not matching is trial, so a campaign that is neither — a retargeting or buffer
campaign — lands on the trial side and dilutes it slightly. Change the brand's
`testing_re` if that matters.

Campaign, ad set and ad rows carry a `seg` tag and are filtered in the browser. Account
and combined rows cannot be filtered, so both segments are rolled up server-side by
summing campaigns per account — which reproduces the blended row exactly. Both brands
reconcile to +0.00 on spend and trials.

Two honest edges the page states rather than hides:

- **Attribution stays brand-wide.** Branch attributes a trial to an ad *name*, which
  carries no campaign, so unmatched trials belong to neither segment. The tile says so.
- **A segment with no spend and no budget is disabled**, not shown empty. An empty table
  reads as "nothing performed", a different claim from "no testing ran this window".

## History store

Settled days are served from GCS instead of being re-fetched. A 30-day window used to mean
a full ad-level insights pull per ad account plus five Branch chunks per event, *every
view*, for days that could no longer change — most of the cold-start latency and most of
the exposure to Meta's code 17.

- Days older than `HISTORY_SETTLE_DAYS` (3) come from the store; today and the two days
  behind it are always live. 30 days → 27 stored + 3 live. 7 days → 4 + 3. Today → all live.
- **A closed day is not a settled day.** Meta bills late and Branch backfills late events.
  A day stored while still moving stays wrong forever, because nothing rechecks it.
- **A day where both sources return nothing is refused**, never written — "no spend" and
  "past retention" are indistinguishable from the client. Both sources answer for
  2026-05-26; neither answers for 2026-02-24.
- **The store is never load-bearing.** No `HISTORY_URL`, or the service down or slow, and
  every path degrades to "nothing stored" and pulls live exactly as before.

### Why a Cloud Run front door

The org policy `iam.disableServiceAccountKeyCreation` forbids downloadable service-account
keys, and Render issues no OIDC identity Google accepts — so no Google credential can live
on Render. `history-service/` runs on Cloud Run with *ambient* credentials; Render presents
a bearer token that is not a Google credential and reaches one bucket. The service also
aggregates before answering, turning a 30-day reply from tens of thousands of per-day rows
into a couple of thousand per-ad rows.

Bucket `admanagementpostly-ads-history` (asia-south1, public access prevented). Service
account `ads-history@` holds `objectAdmin` **on that bucket only** — no project-wide role.

### Accuracy

Verified on a fully settled window across all three brands:

- **Spend reproduces the aggregate call exactly** — 0.00 delta on every brand.
- **Trials** run 0.00%–0.37% low at brand level by day-sum. That difference is *entirely*
  in the unattributed bucket: for Funda, trials matched to an ad name were 84,750 vs 84,772
  (+0.026%) while trials with no ad name were −467. Every CPT is spend over *matched*
  trials, so no ad, ad set, campaign or account figure moves.

Round trip on a real day: write, read back, compare to live — spend delta 0.0000, trials
exact, 768 of 768 ads matching, nothing pulled live.

### Surviving a sleep

The result cache lives in the gunicorn process, and the free instance sleeps after 15
minutes idle — so that cache died with it and the next person paid a full Meta+Branch
build on top of the 15-30s wake. The last good payload is now persisted to the same
bucket, and a woken instance serves it immediately (marked `stale`, with its real age)
while rebuilding behind the request.

    first build            17.0s   (and saved)
    cold after restart      0.5s   restored, rebuilding behind
    cold, no saved copy    10.9s   what every wake used to cost

Writes are throttled to one per ten minutes per window — a build runs whenever the 90s
cache expires and someone is looking, and persisting each one would be a stream of
multi-megabyte writes buying nothing. Saving never blocks a response, and a failed write
is swallowed: the request already has its numbers.

**Note the asymmetry this leaves.** Settled days come from the store, so a 30- or 90-day
window is mostly served from storage. A 3-day window is *entirely* inside the settle
window and therefore 100% live — it is the slowest view, not the fastest. Persisting the
payload cache hides that on repeat visits; it does not change it. The remaining lever is
storing unsettled days provisionally and overwriting them until they settle, which would
change what yesterday's spend means and has deliberately not been done.

### Operating it

    python3 tools/backfill_history.py --days 90          # resumable, skips stored days
    python3 tools/backfill_history.py --dry-run          # what it would fetch

    python3 tools/backfill_channels.py --dry-run         # one-off: split the nameless
    python3 tools/backfill_channels.py                   # pool by channel on stored days

`backfill_channels.py` is a repair pass, not a routine job — days written from now on
carry their channel already. See *Which channel earned a trial* below.

Nightly, three Cloud Scheduler jobs (`ads-history-{postly,speakeasy,funda}`, 03:40/03:50/
04:00 IST) POST `/api/snapshot?brand=X&days=5`. One job per brand so each request finishes
well inside gunicorn's 180s timeout and a throttle on one brand cannot stop the others;
`days=5` with stored days skipped means five missed nights heal themselves. The endpoint is
token-gated because it *spends* rate limit, not because it is private.

## Export

`Export` in the header downloads one CSV per level — **ad accounts, campaigns, ad sets,
ads** — or all four at once. It exists to be acted on, not read, so the file differs from
the table in three ways:

- **Meta ids are included** at every level, plus the parent ids (an ads export carries
  `adset_id`, `campaign_id` and `ad_account_id`). A name is enough to recognise a row on
  screen; it is not enough to act on one, because two ad sets can share a name — the same
  ambiguity that makes Branch split their trials by spend share.
- **Raw figures.** No `₹`, no thousands separators, blank where the table prints an em
  dash. `cpt`, `cost_per_signup` and `cost_per_mandate` are numbers a spreadsheet can
  average; `d0_active_pct` and `d0_cancel_pct` are plain numbers, not `%` strings.
- **Ad and ad set names go out verbatim.** They are the join key back to Meta and to the
  approval sheet, so nothing is stripped or escaped away — not even a leading `=`.

Two details that are easy to get wrong and are handled here:

- Meta object ids are 17 digits, and a spreadsheet reads a bare 17-digit cell as a number
  and silently drops the last two. Pure-digit id columns are therefore written as
  `="120212345678901234"`, the one form Excel and Sheets both keep as text. Account ids
  already start with `act_` and go out plain. A reader that does not want the wrapper
  strips `^="(.*)"$`.
- The file starts with a UTF-8 BOM, because Excel reads a UTF-8 CSV as latin-1 without one
  and plenty of these ad names are not ASCII.

The columns follow the same two absence rules the tables do: a brand with no Branch app
gets no `trials` and no `cpt` column *at all* rather than a column of zeros, and the
Classplus columns appear only when that data exists. Unlike the table, the Classplus
columns ignore the `signups & mandates` checkbox — that switch is there to declutter
something being read, and a file being analysed later has no clutter to save.

`apply the filters below` (on by default) cuts the file by whatever `active only`,
`spend > 0 only` and the name search are set to, at every level — the menu spells out
which ones are in effect, because Export is reachable from the Combined tab where the
filter bar is not even on screen. Each item shows the row count it will write and is
disabled at zero. Filenames are `{brand}_{level}_{window}_{HHMM}IST.csv`.

Rows that sit outside the tables sit outside the export too: Branch trials matching no
live ad, and Classplus signups with no matching ad name. Summing `trials` gives the
attributed total, not `branch_totals`.

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

Read the usage headers before theorising — but know **which edge returns which**,
verified 2026-08-21 against both accounts:

| edge | headers returned |
|---|---|
| `/campaigns`, `/insights`, single-object reads | `x-business-use-case-usage` |
| `/adsets`, `/ads` | `x-ad-account-usage` + `x-app-usage` **only** |

```
x-business-use-case-usage: call_count=1%  total_cputime=6%  total_time=108%
                           estimated_time_to_regain_access=10  tier=development_access
```

Two consequences that are easy to get wrong:

- **`estimated_time_to_regain_access` is not available where it matters.** It only comes on
  `x-business-use-case-usage`, and the ad set and ad listings — the two edges that actually
  get throttled here — do not return that header at all. A code-17 response from
  `act_…/adsets` carries `acc_id_util_pct: 0` and `reset_time_duration: 0`, i.e. no signal
  whatsoever. So for a listing throttle **there is no ETA**, and the page says exactly that
  and falls back to its own 5-minute re-check rather than presenting that interval as a
  promise from Meta.
- **`ads_management` and `ads_insights` are separate quotas.** Both appear as
  `x-business-use-case-usage` with different `type` values, so they must be tracked
  per type rather than collapsed — the roster and the spend call throttle independently.
  Recent live reading: Postly `ads_management` 51%, `ads_insights` 38%.

The ceiling that bites is **total request time**, not call count. So the thing to minimise
is *expensive listings*, not the number of requests. It is also **not per-edge**: when
`act_…/adsets` is throttled, `{campaign_id}/adsets` is throttled too. What survives is
`/campaigns`, `/insights`, and single-object reads — listing ad sets and ads is what gets
cut off. And do not poll a throttled endpoint waiting for it to clear: blocked attempts
still accrue against the window and hold it open.

- **Two Meta calls per refresh, not eleven.** The roster (campaigns, ad sets, ads —
  names, statuses, budgets) is cached for 25 minutes (`ROSTER_TTL`; the ads listing, the
  priciest and least urgent, gets 55 via `ADS_ROSTER_TTL`), because it changes on the
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
- **The retry waits as long as Meta says, when Meta says anything at all.**
  `estimated_time_to_regain_access` sets `_roster_fail_until` where it is supplied; for the
  listing edges, which supply nothing, it falls back to `ROSTER_RETRY` (5 min) and the page
  labels that as its own re-check. Asking early does not work and blocked attempts still
  count against the window, so an eager loop keeps the throttle alive — a 60-second poller
  once held one open for half an hour.
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

### Budget freshness

Spend is re-read on every refresh. **Budgets are not** — they come off the ad set listing,
cached for `ROSTER_TTL` (25 min), so a budget changed in Ads Manager can be up to half an
hour behind the spend figure sitting next to it. Three things follow:

- **The TTLs must not equal the refresh interval.** `_part()` stamps a listing's age when
  its fetch *returns*, so a 30-minute TTL read by a 30-minute tick is always a few seconds
  short of expiry, is served from cache, and the refresh does nothing — the listing then
  refreshes every *other* tick. That is why budgets appeared frozen for up to an hour at a
  time. `ROSTER_TTL` is 1500s and `ADS_ROSTER_TTL` 3300s so each expires with room to
  spare, giving the intended 30- and 60-minute cadences.

- The Spend KPI **states the budget's own age** ("budget as of 01:37") once it is over two
  minutes old, turning amber past 40. Two numbers on one line, one a minute old and one
  possibly half an hour old, must not look equally live.
- **Refresh forces a fresh roster read** (`?hard=1` → `build(force=True)`), so the button
  can actually move a budget figure. It previously could not: `force` only skipped the
  90-second payload cache, leaving the roster untouched for up to 30 minutes. The
  automatic 30-minute pull deliberately does **not** set it: every open tab would then
  force its own roster read, multiplying the one call the roster cache exists to avoid.
  It relies on the TTLs above expiring on their own, which they now do.
- `force` never overrides an **active throttle window**. A Refresh button that hammers a
  throttled endpoint is exactly what keeps a throttle open; during one, the cached roster
  is served and the banner says why.

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

### Which channel earned a trial

Branch attributes trials across **every** channel, not just Meta, so a large part of
what the Attribution KPI used to call "not matched to an ad" was never a data problem
at all — it was Google's.

Branch fills in `~ad_name` for Facebook ads and leaves it empty for Google, which
populates the channel and campaign fields instead. Cross-tabbing `~ad_name` against
`~advertising_partner_name` on 2026-08-20 showed the pattern is clean in both
directions — every named trial was Facebook's, and not one nameless trial was:

| brand     | named (has `~ad_name`) | nameless | of which Google | organic |
|-----------|-----------------------:|---------:|----------------:|--------:|
| postly    |                  1,552 |       40 |              14 |      26 |
| funda     |                 13,628 |   17,070 |          16,322 |     748 |
| speakeasy |                  1,180 |      747 |             689 |      58 |

So each trial is tagged with the partner Branch names for it, and the page reports
Meta / Google Ads / Organic / Other as **measured** counts.

**Google's trials are never apportioned away from Google**, in either mode, and that is
the whole point of reading the partner field. The pro-rata model below shares only the
trials that name *no* channel at all. An earlier version of it put Google's own count in
the pool as well; because the split ratio is derived from Google's count, that made the
arithmetic circular and handed Meta 7,839 of the 16,106 trials Branch said Google earned
on 24 Aug alone — printing a Funda CPT of ₹148.69 where the measured figure was ₹229.09.
The dashboard prints the model's counterfactual next to the breakdown so the size of it
can be checked on whatever window is on screen.

Google **spend** is not read by this dashboard, so Google's trials appear as a count
with no CPT beside them until the Google Ads pull lands.

#### How the tag is stored

Inside the ad-name key, under the prefix `~none~`, followed by Branch's partner name
(empty = organic). That means the store, its aggregator and the persisted payload
cache needed no format change and no migration — a key under the prefix is a nameless
row, and everything else is an ad name exactly as before.

Days written before this existed carry a bare `null` key and are reported as *channel
not recorded* rather than guessed at. `tools/backfill_channels.py` resolves them using
a one-dimension partner query — four or five rows a day instead of hundreds, because
re-running the full ad-name pull across a hundred stored days is what exhausted
Branch's burst limit and took SpeakEasy off the air once already. It rewrites only the
null key; each day's totals come out identical, because the stored nameless count is
redistributed across partners rather than replaced by the second query's own total.
Run on 2026-08-25: 284 days across all three brands, 0 failed.

### Longevity: today is read live

The fold behind the Longevity tab runs twice a day (04:15 / 13:15 IST per brand). Between
runs it would report that every ad set last spent *yesterday* while it was spending — and
the early run happens before most ad sets have spent anything, so for most of the day that
would be the answer however fresh the fold was.

So **Last spend, Days spent and Status are brought up to today** from a live read: one
`level=adset` insights call per account, cached 10 minutes. Ad sets that first spent today
appear in no folded window at all, so they are added from the roster and marked *new
today* — SpeakEasy had 75 of them on 2026-08-26, absent from the tab entirely before this.

**Spend, Per day, Trials and CPT still cover through the fold**, and the note says so.
Today's trials are not fetched here — that is the Branch pull the precompute exists to
avoid — and adding today's spend without them would inflate the CPT of exactly the ad sets
that are running now, which is the direction that gets a working ad set killed. Today's
spend is shown on its own beside the date instead.

`Span` is derived from the dates in the browser rather than read from the artifact: `last`
moves and the artifact's span does not, and `gaps` (span minus days) would go negative and
render as a stop-and-restart that never happened.

### Pro rata — the only reading

There is no measured/modelled switch. **Every trial and cost-per figure on this page is
modelled**, and a gold `Pro rata` badge sits in the header saying so. **Pro rata** shares
the trials Branch could attribute to **no** channel — organic / direct, other partners,
and any day stored before partners were recorded — between Meta and Google in proportion
to each one's share of that day's *attributed* volume.

The one exception is the **Longevity** tab, which stays measured, and says so at the top:
the model lifts every row by the same factor, so it cannot change which creative beats
which, and comparing creatives is the whole point of that tab. Expect its CPTs to read
higher than the ones everywhere else.

Google's own trials are **not** in the pool: Branch names Google as the partner on them,
so they are attributed, and the ratio is what the two claimants measurably earned. The
pool is only what nobody can claim.

It is computed **per day and then summed**, not once over the window aggregate, because
the Meta/Google mix moves. Over Funda's 27 Jul – 25 Aug window the two give ₹136.80 and
₹135.26 — 1.1% apart, which is small but is not nothing and is free to get right.

    per day:  pool(D)      = organic(D) + other(D) + unrecorded(D)
              share_meta   = meta(D)   / (meta(D) + google(D))
              share_google = google(D) / (meta(D) + google(D))
              meta(D)     += pool(D) x share_meta
              google(D)   += pool(D) x share_google
    window:   uplift       = 1 + Σ alloc / Σ meta

Both allocations are computed, not one taken as the remainder of the Branch total. They
agree on every real day, but on a day with **no attributed volume at all** there is no
ratio to apply: both allocations are zero and the pool stays unclaimed, reported as
`unclaimed` in the channel note. Handing it to whichever side the remainder favoured
would assert that a channel earned trials on a day it measurably earned none — the same
mistake as the circular split, just quieter.

The uplift divides by Σ **meta**, not Σ **matched**. Meta's allocation is earned by
Meta's whole measured bucket, but only the matched part of it has ad rows to carry it —
the orphans (trials naming an ad no longer in the account) have nowhere to land, exactly
as in the measured view, where the tables sum to `matched` and not to `meta`. Putting
the whole allocation on the matched rows would credit them with trials the orphans
earned and cut CPT a further 0.02–0.16%.

The uplift reaches the tables as one scalar per event. Every figure on the page is spend
over trials, so multiplying trials is exactly equivalent to re-deriving each row, and it
keeps ad → ad set → campaign rollups exact. **What it cannot do is move trials between
ads** — every row is lifted by the same factor, so the split across rows stays the
measured one. Compare creatives on the measured view.

Where it shows: the gold `Pro rata` badge in the header, `pro rata` on the CPT and Trials
tiles, a note spelling out the size of the lift and the measured figure underneath it,
`prorata` in the export filename, and a trailing NOTE row inside the CSV — a file gets forwarded without the page it came from.

If the model cannot be applied to a window — no Branch app for the brand, a failed Branch
pull, a payload from before this shipped — the badge is **hidden** rather than greyed and
the note says the figures are measured. A gold badge over measured numbers would be a
straight lie about what is on screen.

`?attr=` is accepted and ignored, so old bookmarks still open.

**Payloads are stamped `prorata_model`** and a saved payload whose stamp does not match
the running code is discarded rather than restored. Model 1 put Google's own trials in
the pool and printed a Funda uplift of 1.57 where model 2 prints 1.03; restoring one of
those on a woken instance would have served a 57% error wearing the badge.

Per-day channel totals for stored days live in the `{brand}chan0` store namespace —
four integers per event per day, none of which depend on the current ad roster, so they
are computed once and reused. `snapshot()` folds each new day in as it is written, so
the index cannot fall behind the store.

    python3 tools/backfill_channels.py --audit                  # reads only
    python3 tools/backfill_channels.py --index-only             # fill what is missing
    python3 tools/backfill_channels.py --index-only --reindex   # recompute every day

#### Why the index is written the way it is

The index is one artifact holding every day, so updating it is a read-modify-write — and
the first version of that was unsafe in two ways, both silent.

`history.get_agg()` returns `None` both when nothing is stored **and** when the store
could not be reached. A writer that cannot tell those apart treats a failed read as an
empty index, writes back the single entry it was adding, and destroys the rest — and
because it stamps the artifact with today's date, the truncated one is then the *newest*
and wins every subsequent read. `get_agg_raw()` returns `(artifact, ok)` so writers can
refuse; `chan_index_add()` and `chan_index_build()` both do.

The install backfill also did one read-modify-write **per day** — 286 round trips — and
ignored every return value. It lost one: Postly's `2026-06-01` kept its trial rows but
never gained its install row. Nothing anywhere reported it. The backfills now accumulate
and write **once**, check the result, and read it back to confirm no day went missing.

`--audit` exists because that failure is invisible from the outside: an index that has
quietly lost days looks exactly like a correct one, and the only symptom is the pro-rata
view covering fewer days and printing a smaller uplift. The channel note says how many of the
window's days the model actually covered, for the same reason.

**This is a model, not a measurement.** Branch labels the pool organic or "other", not
Meta's. Pro rata deliberately overrides that, on the argument that last-touch
attribution under-credits upper-funnel Meta spend. Treat the CPT it prints as a
different question, not a better answer to the same one — and note that with Google's
trials out of the pool it now sits within a few per cent of measured, so the ₹150
thresholds stay usable in either mode.

### Caveats the UI states on every view

- **Attribution coverage.** Branch trials whose ad name matches no ad in either
  account sit outside every row, and the Attribution KPI shows what fraction is
  covered. On Postly that is ~95%; on Funda it is ~43%, because more than half of
  Funda's trials are bought on Google. The channel breakdown above the tables says
  which, so a low number is read as "Google is big here" rather than "the pipeline
  is broken".
- **Meta ads no longer in the account.** A trial naming a real ad that has since
  been deleted is Meta's — named means Facebook — but has no row left to attach to.
  Counted under Meta in the breakdown, excluded from every CPT. Tens per day.
- **Shared ad names.** Where several ads share one name, Branch cannot tell them
  apart. Their trials are split by spend share, so rollups stay exact while the
  individual per-ad CPTs are an even-CPT assumption. Those rows are tagged
  `shared name`.
- **Intraday.** Meta spend lags a few minutes and Branch trials keep landing
  through the day, so today's CPT reads high in the morning and settles.

## Classplus (signups & mandates)

Redash query `19695` on `data.classplus.co`, read through its query API key. It returns
one row per **ad name per signup date** over a rolling 30-day window — `signup_date`,
`signups`, `trial_mandates`, `d0_active`, `d0_cancelled` — about 10.4k rows, so any
window inside the last 30 days is answered by slicing one cached result.

**Cost/mandate is a second opinion on CPT.** It is the same rupees-per-trial unit,
built from the product database instead of the attribution SDK, and it is coloured
against the same ₹150 target. On 2026-08-20 the two agreed to within 3% combined and
to within one or two per ad on the top spenders — which is the point of having it.

Three things about this source shape the code:

- **The window is written into the SQL, and it rolls.** The query takes no parameters —
  the result key is read-only (`POST /api/queries/<id>` → 403) and a `parameters` object
  in the POST body is ignored. `_cp_window()` reads the bounds back out of the SQL text
  and handles both shapes it may take: literal dates, or `UTC_TIMESTAMP()` ±
  `INTERVAL n DAY` for a window that moves with today. Ask for a date outside that
  window and the page says so and shows nothing — putting one day's mandates next to
  another day's spend would be worse than a blank column.
- **Per-day only because the query says so.** The dashboard treats a result as a per-day
  table when it selects a signup-date column (`signup_date`, `signup_date_ist`, `date`,
  `day` or `dt`), and only then can it serve a sub-window. Drop that column from the SQL
  and the same query collapses back to a single 30-day block that can answer nothing but
  its own full range. Several queries may be configured (`CLASSPLUS_QUERIES`) and
  whichever can honestly answer the window on screen does; each is fetched and
  failure-cached independently, so one dead query cannot take another down.
- **It is a signup-cohort measure.** `trial_mandates` counts trials taken by people who
  *signed up inside the window*. Branch counts trial events inside the window whoever
  they came from. Someone who signed up yesterday and started a trial today counts for
  Branch and not here — which is exactly why a given day's mandate count keeps creeping
  up for a while after that day ends. That is why the two columns differ slightly, and
  why they stay separate columns rather than being reconciled into one.
- **It is optional.** No key, or a query that fails, and the dashboard behaves exactly
  as it did before. Meta and Branch are what CPT rests on; a third source must never be
  able to block them. Failures are cached the same way roster failures are.

Freshness is negotiated with Redash rather than polled: the fetch POSTs `max_age`
(`CP_TTL`, 600s), so Redash either returns its cached result or starts a run. A run is
polled for at most `CP_POLL_BUDGET` (20s) — the query takes ~15s and a cold page is not
going to wait — and whatever happens the last result is served with its age shown. The
numbers are then read from `results.json`, not from the POST response, because only
`results.json` carries the SQL, and the SQL is the only place the window is recorded.

Configure with `CLASSPLUS_QUERIES="19695:key"` (or a `queries` list in
`~/.anthropic/classplus_creds.json`). The older single-source `CLASSPLUS_QUERY_ID` /
`CLASSPLUS_API_KEY` pair still works and is appended to the list.

## Per-brand links

Each brand has its own unguessable link — `/b/<value>` — because each brand is a different
team. Opening one locks the page to that brand, hides the brand switcher, and every API
call carries the key so the server can tell one team's link from another's.

Separate URLs alone would have been theatre: the switcher was still on the page and
`?brand=` accepted anything, so a Funda link reached Postly's spend in two clicks. The key
is what makes it real.

- A **valid key asking for another brand** is served its own brand, not an error. A stale
  bookmark and someone trying it on deserve the same answer.
- An **invalid key** gets a 403 that names no brands and reveals no count.
- The **bare `/` URL requires a key too** — it is the URL everybody already has, so leaving
  it open would defeat the purpose. `BRAND_LINK_ALL` is the all-brands link; keep it to
  yourself.

**This is not a login, and should not be described as one.** The dashboard has always been
"anyone with the URL" by the owner's decision; this narrows that from one secret covering
three brands to one secret per team. A forwarded link works until it is rotated, and
rotating is changing one env var — no deploy.

Values live in `~/.anthropic/brand_links.json` (mode 600) and as `BRAND_LINK_*` on Render.
With none set, `LINKS_ON` is false and the app is open to all brands exactly as before, so
this can be switched on and off without deploying.

## Access

**There is no login.** Anyone with the URL sees today's spend, CPT, budgets, and
every ad set and ad name across **all seven ad accounts of all three brands**. `.onrender.com` hostnames are public
and guessable, so treat the URL itself as the only control. The app sends
`X-Robots-Tag: noindex, nofollow, noarchive` and a disallow-all `robots.txt` to keep
it out of search results, which stops crawlers but not anyone who has the link.

To turn on a password later, set `ADMIN_PASS` (and optionally `ADMIN_USER`, default
`postly`) in the Render environment — the code path is already there and switches on
by itself when the variable is present. No redeploy needed beyond the restart Render
does when you save an env var.

## Trends and Matrix

Two tabs over the same fold, `/api/series`: one number per row per **day**, for a chosen
dimension. The window tables collapse the days and Longevity only tracks spend, so
neither could draw a trend line or fill a date grid.

**Dimension** — Script (ad name), Ad set, Campaign, Ad account, Stage, Platform.
**Metric** — Spend, Trials, CPT, Installs, CPI.

*Script is the ad name.* Branch attributes to an ad name and nothing else, and the same
creative is rebuilt into new ads across ad sets and builds, so the name groups those
rebuilds where an ad id would not. There is no separate script registry behind it — the
`{creative}_{build}` tail in Postly's names parses on 96% of ads but yields 1,557 ids
across 1,848 ads, so it groups nothing.

*Platform* is the one dimension that can show a non-Meta row: Branch knows which trials
and installs Google earned (see the channel section above), and this dashboard reads no
Google spend, so those rows carry volume with no cost beside them.

### Two things it deliberately does

**These tabs own their window.** A `Last 7 / 14 / 30 / 60 days` selector sits in their
control bar, defaulting to **30 days**, and the page's range picker does not apply to
them — Longevity already works this way. Following the picker meant opening Trends on
its default of "Today" and getting a single point with a paragraph explaining why it was
useless. A trend line over one day is not a smaller answer, it is no answer.

**Today is excluded.** A window ends *yesterday* and takes one extra day at the far end,
so "Last 30 days" is thirty whole days. Today is partial — spend is minutes behind,
Branch trials land all evening — and on a trend line a partial day dives towards zero and
reads as a collapse that never happened. The KPI tiles above the tabs still include
today; the note explains the difference.

**The Matrix shows every row, and sorts by any column.** It pages — 50 / 100 / 250 / 500
/ All, default 100 — rather than showing a top N. The old top 20 was not a summary of the
spend, it was a slice of it: on Postly's 30-day script fold the top 60 rows are **40% of
the spend**, and rank 61 had still spent ₹24,395. Clicking a heading sorts by it, and
that includes each **day** column, which is the question a date grid exists to answer
("who was cheapest on the 21st"). Text columns open A→Z, numbers open biggest-first.

A blank is not a zero, and the sort keeps that distinction: for Spend / Trials / Installs
a day a row did not run really is zero and sorts as one, but a CPT with no trials behind
it has no value at all and sinks to the bottom in **both** directions — otherwise "sort
ascending by CPT" would answer "who was absent" instead of "who was cheapest".

The footer carries two totals, because with paging one number cannot mean both things:
**this page**, which adds up the column above it, and **all rows**, which is the window.
Export CSV writes every row matching the filter in the order on screen — not just the
page, which would be a screenshot rather than an export.

**Active only.** A checkbox in the control bar keeps just the rows still running. It
applies to the dimensions that are a thing which can be paused — **Ad set**, **Campaign**
and **Script** — and is disabled, with the reason on it, for Stage, Platform and Ad
account, which are buckets rather than objects. A *script* counts as running while **any**
live ad still carries that name.

Live/paused is stamped on the rows at **serve** time, never folded into the artifact. A
fold is cached fifteen minutes and persisted to the store for far longer; an ad set paused
in between would keep reading active — the identical mistake the Longevity tab made with
its last-spend date. And a roster Meta will not return leaves status **unknown**, which
disables the filter rather than emptying the grid: an empty set rendered as "nothing is
running" is the worst possible way for this particular control to be wrong.

Verified against `/api/data` in both directions: of 1,437 Postly ad-set rows in a 14-day
window, 110 are running, with zero rows flagged active that the payload calls paused and
zero the other way. The stored artifact carries no `active` field at all.

**Derived metrics come from the sums.** CPT for a period is total spend over total
trials, never the mean of the daily CPTs — on a row that ran on three days out of
fourteen those are far apart and only the first is the real cost. A day with no trials
renders as a dash and *breaks* the line rather than being drawn as zero.

### Cost

The fold is 16–26s cold: raw stored days plus a live pull for the unsettled tail. So the
result is cached in process for 15 minutes, served stale while it refreshes, **and
persisted to the store** under `{brand}ser{dim}` — the in-process cache dies with the
process, which on the free plan is every fifteen idle minutes. A cold instance restores
in 0.7s.

Returning every row makes a fold 1.5–2 MB (Postly, 30 days, script: 4,136 rows, 1.69 MB),
so two things had to change with it. Responses are **gzipped** in an `after_request` —
it is nearly all digits and repeated keys and compresses **9.9×**, so that fold is 175 KB
on the wire. And the in-process series cache is now bounded (`SERIES_CACHE_MAX`, default
8, least-recently-used evicted): an unbounded dict of 2 MB folds is an OOM on a 512 MB
instance. `SERIES_TOP` still exists and still caps the fold, but defaults to `0`, meaning
no cap; `SERIES_MAX_ROWS` (20,000) is a safety net against a dimension nobody has tried,
and when it bites the note says the fold was capped.

The nightly precompute warms the default combination (14 days, script) for each brand.
A stored artifact is reused only when its dates match the request **exactly** — a window
that has rolled forward a day is a different question — **and** its `row_cap` matches the
running code. Dates alone was not enough: on the first deploy of the uncapped fold,
SpeakEasy restored a 60-row artifact the previous code had written (right dates, wrong
rows) and silently put the truncation back.

Both tabs are deep-linkable, sort and page size included:
`?tab=matrix&win=30&dim=adset&metric=cpi&per=250&sort=2026-08-21&dir=asc&active=1&testing=1`. A `sort` that
names no column the current grid has falls back to the period total rather than sorting
by nothing, so a stale bookmark degrades instead of breaking.

### Installs

`eo_install` from Branch, joined to ads by the same ad-name key as trials and stored as a
third pseudo-event (`inst`) in the same `{event: {ad_name: n}}` shape — so the store, its
aggregator and the channel index needed no format change. Installs are pulled on the same
trip as the trials rather than in a second pass.

They are kept out of `matched`, `channels` and `unattributed`, which describe *trial*
attribution — the thing every CPT divides by. Pro rata does not touch them either: that
model is about trial attribution.

`tools/backfill_installs.py` fills days stored before this existed. Only the install
series is fetched — one query per 7-day chunk rather than a full re-pull — so the whole
history is about forty requests. Run 2026-08-26: 286 days across three brands, 0 failed.

## Pre-deploy checks

    ./tools/check.sh

Python syntax across every module, then `node --check` over the page's inline
`<script>`. The second one earns its place: the entire front end is one inline script
in a Jinja template, nothing compiles it, Flask serves a broken one happily, and a
stray newline inside a quoted string produces a page with a perfectly good header and
no data underneath. `import server` cannot see that, and neither can any test that
only exercises the API.

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
   so they are never in git): `META_TOKEN`, `BRANCH_KEY`, `BRANCH_SECRET`, and the
   per-brand Branch pairs listed under [Credentials](#credentials).
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
| `config.py` | the `BRANDS` table (accounts, events, targets) + credential resolution |
| `static/brand/` | one logo per brand — swap a file here to change a logo |
| `history.py` | client for the settled-day store (degrades to "nothing stored") |
| `history-service/` | the Cloud Run front door to the GCS bucket |
| `tools/backfill_history.py` | fill the store with settled days |
| `tools/backfill_channels.py` | one-off: tag stored nameless trials with their channel |
| `tools/backfill_installs.py` | one-off: add the install series to older stored days |
| `tools/check.sh` | pre-deploy syntax checks, including the page's inline JS |
| `Dockerfile`, `render.yaml` | deploy |

## Credentials

Nothing secret is committed. `config.py` reads, first hit wins:

1. env — this is what Render uses:
   - `META_TOKEN` — one token, all seven ad accounts across the three brands
   - `BRANCH_KEY` / `BRANCH_SECRET` — Postly (unprefixed for backwards compatibility)
   - `SPEAKEASY_BRANCH_KEY` / `SPEAKEASY_BRANCH_SECRET`
   - `FUNDA_BRANCH_KEY` / `FUNDA_BRANCH_SECRET`
   - `CLASSPLUS_QUERIES` / `CLASSPLUS_HOST`
2. `~/.anthropic/meta_token`, `~/.anthropic/branch_creds.json`,
   `~/.anthropic/classplus_creds.json` — local runs. The Branch file is keyed by brand:
   `{"postly": {"branch_key": …, "branch_secret": …}, "speakeasy": {…}}`; the older flat
   `{"branch_key": …}` form is still read and treated as Postly.
3. `~/Desktop/Postly Ads Management/postly_config.py` — last-resort local fallback

**Meta is the only hard requirement.** It is the sole source of spend, and spend is what
every other number divides by, so a missing token is a startup error. Everything else is
allowed to be absent and degrades in place: no Branch pair for a brand drops its trial
and CPT columns, no Classplus key drops the signup and mandate columns.

It cannot rely on (3) alone: macOS protects `~/Desktop` and a process launched as a
server is not granted access to it, so that import fails at serve time even though it
works from a shell. If a token is rotated, update `~/.anthropic/` **and** the Render
env var.

### What the tiles deliberately do not carry

The KPI row states the number and labels it `pro rata`. It does **not** show the lift
factor, the measured figure under it, or an attribution percentage — those moved to the
note. A tile is where somebody reads a number off at a glance; the arithmetic behind it
belongs one paragraph down, where there is room to say what it means. The `Meta share ·
modelled` and `Attribution` tiles were removed outright for the same reason: the full
channel breakdown, with counts and percentages for every channel, is in the note.

## Ads Manager links

Every row that **is** a real Meta object links to it, opening in a new tab on the same
window the page is showing (`&date=<since>_<until>`) — landing on a different window is
how people end up believing the dashboard and Ads Manager disagree.

| view | opens |
|---|---|
| **Ads** | **the creative itself** — see below |
| Ads (the small ↗) | `manage/ads?act=…&selected_ad_ids=…` |
| Ad sets, Longevity, Matrix `dim=adset` | `manage/adsets?act=…&selected_adset_ids=…` |
| Campaigns, Matrix `dim=campaign` | `manage/campaigns?act=…&selected_campaign_ids=…` |
| Ad accounts, Matrix `dim=account` | `manage/campaigns?act=…` |

Two rules the code holds:

**No link is better than a dead one.** Ads Manager resolves an object id only inside an
`act=`, so a row that does not know its account renders as plain text. `fbUrl` returns
`null` rather than guessing, and `fbName` falls back to the bare name.

**Name dimensions never link.** Matrix `dim=script` is an ad *name*, which maps to many
ads by design — that is the entire reason the dimension exists — and `stage` / `platform`
are buckets, not objects. In the Matrix a row carries the account that spent most on it
(`acct`), because a script name can appear in both of a brand's accounts and a link into
the wrong one is worse than none.

Folded rows gained that field, so series artifacts carry a `shape` stamp checked
alongside `dates` and `row_cap` — a fold from before the change would give the Matrix a
linkless grid that looks exactly like a broken link.

Links are underlined on hover only: a table where every name is permanently underlined
reads as noise.

### The ad creative

Clicking an **ad name** opens the rendered ad — the actual image or video with its copy,
as it runs — not Ads Manager. That is what somebody scanning a list of ad names wants to
see. Ads Manager stays one click away as a small `↗` beside the name: the creative is the
common case, the manager is the occasional one. Every level above an ad (ad set,
campaign, account) has no single piece of media, so those still open Ads Manager.

It goes through `/api/preview?ad=<id>&brand=<b>&k=<key>`, which asks Meta for
`previews.ad_format(MOBILE_FEED_STANDARD)` and **302s** to the URL it returns.
A redirect rather than a URL baked into the payload, for three reasons:

- Meta's preview link is **signed and expires**, so one folded into a payload restored
  from the store hours later would be dead — the one thing the link rules forbid.
- It is ~530 bytes per ad. On an 1,866-ad brand that is another megabyte in every payload,
  paid whether or not anyone clicks.
- It is a **credential-bearing URL**, and it should not sit in the page for two thousand
  ads at once.

Resolved previews are cached in process for 15 minutes and the cache is bounded.

Two things the route holds:

**A team link cannot read another brand's creatives.** The ad's account comes back from
Meta on the same request — so the caller cannot spoof it — and an ad in no account the
brand owns is refused with a 403, not previewed.

**`Referrer-Policy: no-referrer` on the redirect.** Without it the browser hands
business.facebook.com a `Referer` containing this request's URL, and that URL contains the
team's link key. The point of a per-team secret link is that it does not travel to third
parties.

When Meta renders no preview — deleted creative, a placement this ad does not run in — the
route serves a plain page saying so, with a link onward to Ads Manager. A blank tab would
look like the dashboard was broken.
