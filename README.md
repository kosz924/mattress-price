# mf-price-watch

A tiny, dependency-free price watcher for Mattress Firm product pages, designed
to run as a scheduled GitHub Action. It fetches each product, extracts the
current (and original) price, logs a history, and alerts you when a price drops.

## How it works

- `check_price.py` — fetch + extract + compare + alert. **Standard library only.**
- `config.json` — the products to watch and the alert rules.
- `data/price_history.json` — persisted state; the workflow commits it back each
  run, so it doubles as a price log you can `git log`.
- `.github/workflows/price-check.yml` — runs every 6 hours (and on-demand),
  commits history, and opens a GitHub issue when an alert fires.

Extraction tries three sources in order: the `__NEXT_DATA__` JSON blob (Mattress
Firm is a Next.js site), then JSON-LD, then a regex fallback that reconstructs
the split-cents DOM (e.g. `$4,099` + `00`).

## Setup

1. Create a new repo and drop these files in.
2. Push. That's it for the default (GitHub-issue) alerts.
3. Trigger a first run: **Actions → price-check → Run workflow**.

### Verify the first run

Run it once locally with `--dump` to confirm the price parsed correctly and see
exactly which field it came from:

```bash
python check_price.py --dump
```

If it prints `ERROR: no price found`, the `--dump` output lists every
price-bearing field it saw (path → value) so you can confirm the site's field
names. The selectors in `CURRENT_KEYS` / `WAS_KEYS` cover the common ones.

## Alert modes (`notify_on` in config.json)

- `any_drop` — alert whenever the price is lower than the last check (default).
- `below_target` — alert when price ≤ `target_price`.
- `on_sale` — alert when a strikethrough/"was" price is detected above the
  current price.

## Optional: Slack / Discord

Add a repo secret `ALERT_WEBHOOK_URL` (an incoming-webhook URL). The script
posts the same alert text there in addition to the GitHub issue. The payload
includes both `text` (Slack) and `content` (Discord) keys, so either works.

## Watching more products

Add objects to the `products` array. Get the `variant_id` from the page URL
(`?variantid=...`); it scopes extraction to the right size when the page embeds
all variants.

## Heads-up: bot blocking

GitHub-hosted runners use Azure datacenter IP ranges, which large retail CDNs
sometimes challenge or block. The fetcher sends a browser-like User-Agent and
detects obvious challenge pages, but if you start seeing fetch errors:

- Run on a **self-hosted runner** on an always-on machine (a residential IP is
  far less likely to be challenged). Add `runs-on: [self-hosted]` to the job.
- Or route the fetch through a scraping API (ScrapingBee, ScraperAPI, Zyte) by
  swapping the URL in `fetch()`.

A self-hosted runner is the simplest reliable option if the hosted runner gets
blocked.

## Notes

- GitHub cron is best-effort and runs in UTC; expect occasional delays.
- History is capped at the last 200 distinct price points per product.
