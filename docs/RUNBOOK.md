# Runbook

Operations for the Ads Performance dashboard. Read `docs/HANDOFF.md` first for the map.

Every ops endpoint below needs the bearer token:

```bash
T=$(cat ~/.anthropic/ads_history_token)
BASE=https://postly-cpt-dashboard.onrender.com
curl -s -H "Authorization: Bearer $T" "$BASE/api/..."
```

Read-only API calls need a brand link instead: `?k=$(python3 -c "import json;print(json.load(open('$HOME/.anthropic/brand_links.json'))['all'])")`

---

## Deploy

```bash
cd ~/postly-cpt-dashboard
git add -A && git commit -m "..." && git push origin main    # the GitHub Action ships it
```

Render's own GitHub connection is not wired for this account (see `HANDOFF.md`), so
`.github/workflows/deploy.yml` calls Render's API on every push to `main` and then waits
until `/healthz` names the pushed commit. Watch it with `gh run watch` or:

```bash
gh run list --repo krishnaprasun/postly-cpt-dashboard --limit 3
curl -s https://postly-cpt-dashboard.onrender.com/healthz   # {"commit":"<sha>", ...}
```

Docs-only pushes deliberately do not deploy. To ship one anyway, use **Run workflow** on
the Actions tab.

**Editing the workflow: validate before pushing.** A YAML parse error produces a run with
**zero jobs, no log and no annotations** — it just says `failure`, which reads like a
deploy failure and is not one. The classic cause is a continuation line inside a `run: |`
block starting at column 0, which breaks out of the block. Check both layers first:

```bash
ruby -ryaml -e 'YAML.load_file(".github/workflows/deploy.yml"); puts "YAML OK"'
```

**Before every push: scan for secrets.** The repo is public.

```bash
git diff --cached | grep -inE 'EAA[A-Za-z0-9]{20}|key_(live|test)_|1//[A-Za-z0-9_-]{30}|rnd_[A-Za-z0-9]{20}' && echo "STOP" || echo "clean"
```

Watch the deploy:

```bash
RK=$(cat ~/.anthropic/render_key)
curl -s -H "Authorization: Bearer $RK" \
  "https://api.render.com/v1/services/srv-da3cttibkg8c738a4nvg/deploys?limit=3" |
  python3 -c "import json,sys;[print(d['deploy']['id'],d['deploy']['status'],d['deploy']['commit']['message'][:50]) for d in json.load(sys.stdin)]"
```

**The Render trap that cost 20 minutes once:** Render logs `==> Your service is live 🎉`
and the API reports `live` **before** it has detected the open port. Until the
`==> Detected service running on port NNNN` line appears — five minutes later, on one
deploy — every request returns a bodiless `text/plain` **404**, not a 502 and not a wake
screen. Tell them apart by the `rndr-id` response header: present means the request
reached the app, absent means it died at Render's edge. Don't debug the app during that
window; look for the port-detection line in the logs first.

Env vars change with no deploy — but Render env **overrides code defaults**, and one of
them (`CACHE_TTL=90`) silently defeated a code change once:

```bash
curl -s -X PUT -H "Authorization: Bearer $RK" -H "Content-Type: application/json" \
  -d '{"value":"780"}' \
  "https://api.render.com/v1/services/srv-da3cttibkg8c738a4nvg/env-vars/CACHE_TTL"
```

---

## Verifying a UI change

**Do not grep a DOM dump for an error string.** This template's *source* contains the
words `ReferenceError`, `loading` and every class name you might search for, so a grep
matches on a healthy page and on a broken one alike. That mistake shipped a live
`ReferenceError` that broke the Google and Blended views — twice.

Drive a real browser and read `Runtime.exceptionThrown` over CDP instead. Count rendered
elements by class, not by substring.

---

## Playbooks

### The page shows a dash where a number should be

Working as designed, in almost every case. A dash means *unknown*, never *zero*. Check
which: Branch refused (rate limit), the day predates the field being stored, or the
credential is down. `/api/google/status` answers the Google half directly.

### Google figures vanish

```bash
python3 tools/google_ads_check.py
```

Four things must be true and they fail in ways that look identical from outside — an empty
result can mean *no spend*, *test access only*, *wrong account*, or *token expired*. The
check tests them in order and stops at the first failure.

- `invalid_grant` → the refresh token died. Publish the consent screen in project
  `734843757980`, then `python3 tools/google_ads_token.py` and update the Render env var.
  A token minted while the screen is in Testing lasts 7 days no matter how it is stored.
- `PERMISSION_DENIED` on every account → `login_customer_id` is wrong. It names the
  **manager you act through**, never the account you are reading. The correct value is the
  Testbook MCC `3343252288`. This looks exactly like a test-level developer token, and was
  misdiagnosed as one for an hour.
- Everything passes but spend is empty → genuinely test-level developer access.

`listAccessibleCustomers` returns what the *user* reaches — one manager — not the accounts
beneath it. Expanding to the real 18 needs `customer_client`.

### Meta calls start failing

Codes **4, 17 and 613** are rate limits, and they bind on request *time*, not request
count. The app degrades rather than erroring: budgets and statuses go unknown while spend
and trials keep working. Speakeasy's accounts run hot from its own automation, so expect
degraded reads there more often.

Nothing to do but wait, unless it is persistent — in which case the fix is Meta Standard
Access, not more retries.

### Branch starts returning 429

Branch throttles per app key and caps a page at 1,000 rows. A backfill of mine once
exhausted the quota and took the dashboard's own numbers down with it. **Run backfills
overnight, with a budget, never during the day.**

### The instance is asleep / first load is slow

Expected on the free plan: ~15 min idle → spin-down, 15–30s cold wake. The app answers a
woken request from its last cached numbers and refreshes behind it, so the wake shows real
figures rather than a blank page.

**Do not add a 24/7 keep-alive pinger.** Render's free tier is 750 instance-hours per
month **per workspace**, and this workspace runs three free web services. Keeping one
awake round the clock is 744 h, which exhausts the pool — and Render's response is to
suspend *every* free web service in the workspace until the 1st. A windowed keep-alive was
offered and declined on 2026-08-23; cold opens are the accepted trade. The real fix, if it
ever comes up again, is Starter at $7/mo.

### A stored day looks wrong

Nothing rechecks a stored day, so a day stored while still moving stays wrong forever.
`HISTORY_SETTLE_DAYS=3` exists for exactly this. A day where **both** sources return
nothing is refused and never written, because "no spend" and "past retention" are
indistinguishable.

To re-store a range, `tools/backfill_history.py --brand X --since ... --until ...`
(resumable, skips stored days, refuses unsettled ones).

### Numbers on screen don't match the code you just shipped

Cache layering. In order of likelihood: the browser's IndexedDB copy (bump `APP_VERSION`
by touching a served file), the server payload cache (wait out `CACHE_TTL`, or `?force=1`),
a stored artifact whose shape changed (bump `SERIES_SHAPE` / `row_cap` / `PRORATA_MODEL`),
or a Render env var overriding the code default.

---

## Routine tasks

**Add a brand:** one entry in `BRANDS` in `config.py` (accounts, Branch events, labels,
CPT target, theme), one Branch key pair in `~/.anthropic/branch_creds.json` and Render,
one `BRAND_LINK_*`. No other file needs to change.

**Change a CPT target:** `cpt_target` in `BRANDS`. `None` means *show the number, do not
colour it* — a red cell reads as an instruction, so an unagreed target is worse than none.

**Rotate a brand link:** change the Render env var. No deploy.

**Delete the finished backfill jobs:**

```bash
for b in postly speakeasy funda; do
  gcloud scheduler jobs delete ads-reach-backfill-$b --location=asia-south1 --quiet
done
```

**Check backfill progress before deleting:**

```bash
curl -s -H "Authorization: Bearer $T" "$BASE/api/backfill/google?brand=postly&budget=10&dry=1"
# pending_after: 0 across all brands means it is done
```

---

## The CSS trap

The semantic `.good` / `.warn` / `.bad` / `.dimtxt` rules **must stay at the very bottom**
of the `<style>` block with their `.kpi .v.x`, `td.x` and `tfoot td.x` variants. `.kpi .v`,
`td` and `tfoot td` each declare a colour and out-specify a bare `.bad`, which silently
turned the CPT colour coding off with no error — it just rendered navy. After any restyle,
verify by reading the *computed* colour, not by eyeballing.

The good/warn/bad colours are never themed per brand. A "good" CPT has to stay green
everywhere or the one colour anyone acts on stops meaning one thing.
