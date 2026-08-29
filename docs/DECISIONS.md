# Standing decisions

Things that look like bugs, omissions or easy wins, and are none of those. Each was
decided deliberately, most of them after getting it wrong the other way first. Reverse any
of them if there is a reason — but know what you are reversing.

---

### No login

The owner was told plainly that a public `.onrender.com` URL exposes live spend, budgets
and every ad and ad-set name for seven accounts, and chose no login anyway. **Do not
re-litigate this unprompted.**

What shipped instead: `noindex`, a disallow-all `robots.txt`, and per-brand links.
`ADMIN_PASS` switches HTTP basic auth on with no code change — path already tested, 401
without, 200 with, `/healthz` stays public for the health check. Offer it if the exposure
ever becomes a real problem; don't offer it again otherwise.

### Pro rata is the only mode, and there is no badge

There was a measured/pro-rata switch. The owner removed it: *"pro rata one is of no use for
me"* meant the *measured* reading was useless, and then *"no no nos. should be pro rata
only don't want badge in the UI"*. The numbers are always pro rata and the page does not
announce it. The explanation lives in the info panel.

Google's own measured trials are **not** in the pool being divided. Putting them back makes
the allocation circular — it was the original bug.

### A missing value renders as a dash, never a zero

"We do not know what this cost" and "this cost nothing" are opposite claims. Applies to
impressions, budgets, trials and active status. Two live bugs came from collapsing the
distinction, the worse of which reported a blended CPT of ₹354 against a true ₹191 —
because a guard added to Google's missing trials was not added to the identical hole on
Meta's side.

### An unagreed CPT target is worse than none

`cpt_target: None` renders the figure uncoloured. A red cell reads as an instruction, and
borrowing another brand's target to have *something* to colour against is how you get
someone acting on a number nobody agreed to. Targets today: Postly ₹150, Speakeasy ₹275,
Funda ₹180.

### Testing and trial spend are never blended

A testing campaign is looking for a creative that works and is expected to cost more per
trial; a trial campaign is scaling one that already does. Blending them produces a CPT
that describes neither, and judging a testing ad set against the trial target kills the
pipeline that feeds it. Split by `testing_re`, per brand, verified across all seven
accounts.

### One gunicorn worker

The payload cache is in-process. A second worker keeps its own copy, doubling Meta and
Branch calls while halving the hit rate. Threads carry the concurrency instead.
`--timeout 180` because a 7-day pull is ~30s locally and slower on a free instance's
shared CPU; gunicorn's 30s default kills it mid-pull.

### No 24/7 keep-alive

Render's free tier is 750 instance-hours per month **per workspace**, and this workspace
runs three free web services. Pinging one around the clock is 744 h in a 31-day month,
which exhausts the pool — and Render suspends *every* free web service in the workspace
until the 1st. A windowed 09:00–23:00 keep-alive was offered and declined on 2026-08-23.
Cold opens costing 15–30s are the accepted trade.

### The 15-minute refresh is unforced

The old 30-minute cycle used `force`, which bypassed the server cache — so every open tab
paid for its own Meta + Branch rebuild. Unforced at 15 minutes makes *fewer* upstream calls
than forced at 30, because the first tab past the TTL triggers one rebuild and everyone
else is served from it. Hidden tabs skip the pull entirely. The `/healthz` keepalive does
not skip hidden tabs, because keeping the instance warm is the one thing worth doing while
nobody is watching.

### A closed day is not a settled day

`HISTORY_SETTLE_DAYS=3`. Nothing rechecks a stored day, so a day written while still moving
is wrong forever. Storing unsettled days provisionally would make 3-day windows fast — they
are currently the *slowest* view, being 100% live — but it would change what yesterday's
spend means. Deliberately not done.

### A day where both sources return nothing is refused

"No spend" and "past retention" are indistinguishable from the outside. Both sources
answer 2026-05-26; neither answers 2026-02-24. Writing a zero for the second one would
manufacture a fact.

### Ad sets renames itself to Ad groups on Google

Same slot, one rung below campaign. Two buttons meaning the same thing on different
channels is worse than one that renames itself. Tabs that mean nothing on a channel are
**hidden**, not greyed — Google buying has campaigns and ad groups and nothing below them.

### Two CPTs on Google, never averaged, never blended

The Google channel divides one spend by two counts of the same event: Branch's, and
Google Ads' own. They disagree — by 2% on Speakeasy, 15% on Funda, 72% on Postly — because
each attributes from what it saw, over its own lookback. Averaging them would invent a
third number that is nobody's measurement.

Only the **Branch** CPT is judged against the target: the target was agreed against
Branch's definition, and it is what every Meta tab and the Blended view already count.
Google's number sits beside it to be compared, not mixed in. **Blended deliberately keeps
Branch on both sides** rather than taking Google's own count for the Google half.

Counted from Google's **Conversions** column, never **All conversions**. The same event
usually arrives twice — once from Branch, once from Firebase — and only one feed is marked
primary. `all_conversions` counts both and reports a CPT well below the truth. The match
from Branch event to Google conversion action is on the name, after stripping the
timestamp Google appends when an event is imported twice; two of the three brands are
named that way, so an exact-suffix test finds nothing at all and reads on the page as
"Google reported no conversions".

### Hook rate divides by video impressions, not all impressions

Video was added to the pull a day after impressions, so days settled in between hold
impressions and no video. Both rates therefore divide by `vimp` — the impressions of rows
that actually reported video — and a day without video is blank, never a zero hook rate.
On the trend it draws as a gap. Same rule as CPM dividing `imp_spend` rather than total
spend, and for the same reason: a rate must not be diluted by the part of the window it
never measured.

The 3-second count has no field of its own in v21; it comes from the `video_view` entry
inside `actions`, narrowed with the `filtering` parameter. That filter was checked against
an unfiltered pull first — same 604 rows, same ₹258,000.84 — because if it had dropped
rows it would have deleted spend, not just video.

### The Google creative count is assets, not ads, and is not windowed

A Google ad group has exactly one ad — App campaigns put the variety in the assets. So the
column counts serving video and image assets, not `ad_group_ad`, which would read 1 on
every row. Retired assets are counted apart from live ones, because a group holding 98 live
and 488 retired creatives is not running 586.

It is also the one column on that table that is **current state rather than the selected
window**: assets carry no date on this resource. Reading it as "creatives that ran during
this window" would be wrong, so the header, the tooltip and the info panel each say so.

### Blended stops at the level where the two are comparable

A Meta ad set and a Google ad group are not rows of the same table. Blended is the only
view where the two channels are added, and it has no per-row breakdown for that reason.

### No build step

`templates/index.html` is served as-is — no framework, no bundler, no npm. It keeps the
whole UI editable by anyone who can read HTML, and it makes a deploy a `git push`. 3,839
lines is a lot for one file; it is still the right trade here.

### Every third source is optional

Meta is the one hard requirement, because it is the only source of spend and spend is what
every other number divides by. Branch missing degrades a brand to Meta-only, which is a
legible state. Google, Classplus and the history store are each *never load-bearing*: with
no credential, an expired one, or the service down, they return empty, say why, and the
page renders as it did before they existed.

### The link is not a login

Per-brand links narrow one secret covering everything to one secret per team. A forwarded
link keeps working until rotated. Say "access link", never "login".

### The summary divides by the brand total; the tables sum their rows

Branch attributes a trial to an ad NAME, and a name only becomes a table row when that ad
also spent inside the window. Nearly all of them do, until an account stops delivering —
then the trials keep arriving from installs those ads had already bought, and the ads they
name no longer have a row. Funda, 2026-08-28: three accounts fell from ₹25.9L/day to ₹1.4L
when 114 ads hit Page Privacy Check Fail, 99 were paused for a compromised user and 57
failed a privacy check; 332 of 454 Meta trials then named an ad with no spend that day, and
the headline read ₹1,170 against a true ₹315.

So the KPI tiles divide by Meta's whole bucket (`channels[ev].meta`, or `prorata[ev].meta`
when the model is on). **Do not "fix" this back to the row sum.** The error it removes runs
in the one direction that matters: it made a HALTED account look like an expensive one,
which is the reading that gets working ad sets killed. It also made `vs prior` compare two
different measures — `prior_window` has always used the channel index, i.e. the total.

The tables stay row-based, because a trial whose ad has no row has nowhere to sit and
spreading it would credit ads with trials they did not earn. Where the two differ by more
than 2% the Trials tile says so and a note explains it; below that the tile keeps the event
name. On a segment the off-row trials are shared out by that segment's share of the trials
that do have rows — testing/trial is a campaign property and an off-row trial has no
campaign — and the segments still sum to the brand exactly.

### Google sign-in sits beside the team links, not instead of them

Added 2026-08-29, on the owner's ask. Until now the only credential was a URL: a link per
team, plus `ROOT_OPEN=1`, which left the bare `.onrender.com` address open to anyone
holding it — every brand, full controls.

Sign-in is OpenID Connect against Google, restricted to a Workspace domain, with an
address→brands map in `GOOGLE_AUTH_MAP`. There is no user table, no password and nothing
to reset: Google says who you are, the map says what that address may see, and the map is
re-read on **every** request, so removing someone takes effect at once rather than
whenever their cookie happens to lapse.

The link wins where it is valid. A funda team link opened by the owner still shows Funda
and nothing else — the link is a statement about the view, not about the person, and two
credentials on one request should narrow, never widen.

`full` — the right to force a Meta roster re-read or a longevity recompute, the two things
that make the app SPEND — follows seeing every brand, exactly as it does for links. A
one-brand viewer is read-only.

It ships dark: with no `GOOGLE_AUTH_CLIENT_ID` none of it is reachable and the app is what
it was. Switching it on is three env vars and setting `ROOT_OPEN=0`; switching it off is
unsetting them. No deploy either way — except that on this service an env change does not
redeploy on its own, so one has to be triggered.
