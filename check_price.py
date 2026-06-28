#!/usr/bin/env python3
"""
Mattress Firm price watcher.

Fetches each product URL in config.json, extracts the current (and, if present,
the original/"was") price, compares against the last-seen value in the state
file, and emits an alert when a configured condition is met.

Extraction strategy (in priority order):
  1. __NEXT_DATA__  (the JSON blob Next.js embeds; most reliable)
  2. JSON-LD        (<script type="application/ld+json"> Product/Offer)
  3. Regex fallback (handles the split-cents DOM, e.g. "$4,099" + "00")

The script is intentionally chatty in --dump / --selftest mode so you can verify
which path produced the number on the first real run and pin it down if needed.

No third-party services required. Slack/Discord alerting is optional via env var.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

CURRENT_KEYS = (
    "saleprice", "currentprice", "finalprice", "nowprice", "customerprice",
    "yourprice", "price", "unitprice", "displayprice", "saleamount",
)
WAS_KEYS = (
    "listprice", "wasprice", "msrp", "originalprice", "regularprice",
    "baseprice", "strikethroughprice", "compareatprice", "wasamount",
)

# ----------------------------------------------------------------------------- fetch


def fetch(url: str, timeout: int = 30, retries: int = 3) -> str:
    scraper_key = os.environ.get("SCRAPER_API_KEY")
    if scraper_key:
        import urllib.parse
        target_url = f"http://api.scraperapi.com/?api_key={scraper_key}&url={urllib.parse.quote(url)}"
        headers = {}
    else:
        target_url = url
        headers = {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        }
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(target_url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                charset = resp.headers.get_content_charset() or "utf-8"
                html = body.decode(charset, errors="replace")
            if not scraper_key and _looks_like_bot_block(html):
                raise RuntimeError("response looks like a bot/challenge page")
            return html
        except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError) as e:
            last_err = e
            if attempt < retries:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"failed to fetch {url}: {last_err}")


def _looks_like_bot_block(html: str) -> bool:
    lowered = html[:4000].lower()
    needles = ("access denied", "request unsuccessful", "captcha",
               "are you a human", "/_incapsula_", "akamai reference")
    return any(n in lowered for n in needles) and len(html) < 8000


# ------------------------------------------------------------------- price candidates


def _to_float(v) -> float | None:
    if isinstance(v, (int, float)):
        f = float(v)
        # Heuristic: an integer like 409900 with no decimal is almost certainly
        # cents (Next commerce stores often do this). Treat >100k as cents.
        return f / 100 if (isinstance(v, int) and f >= 100000) else f
    if isinstance(v, str):
        m = re.search(r"[\d,]+(?:\.\d+)?", v)
        if m:
            try:
                return float(m.group(0).replace(",", ""))
            except ValueError:
                return None
    return None


def _walk(obj, path=""):
    """Yield (path, key, raw_value) for every leaf under a price-ish key."""
    if isinstance(obj, dict):
        for k, val in obj.items():
            kp = f"{path}.{k}" if path else k
            if isinstance(val, (dict, list)):
                yield from _walk(val, kp)
            else:
                yield kp, k, val
    elif isinstance(obj, list):
        for i, val in enumerate(obj):
            kp = f"{path}[{i}]"
            yield from _walk(val, kp)


def _variant_prefixes(data, variant_id: str) -> list[str]:
    """Path prefixes of dict nodes whose id/sku/code value == variant_id."""
    id_keys = ("id", "variantid", "sku", "code", "skuid", "itemid")
    prefixes = []
    for kp, key, val in _walk(data):
        if key.lower() in id_keys and str(val) == str(variant_id):
            # strip the trailing ".id" to get the node's own path
            prefixes.append(kp.rsplit(".", 1)[0] if "." in kp else "")
    return prefixes


def _collect_prices(data) -> list[dict]:
    out = []
    for kp, key, val in _walk(data):
        kl = key.lower()
        if "price" not in kl and "amount" not in kl:
            continue
        if any(skip in kl for skip in ("priceid", "pricetype", "pricerange",
                                       "priceper", "pricegroup")):
            continue
        f = _to_float(val)
        if f is None or f < 50:  # ignore shipping/financing fragments
            continue
        out.append({"path": kp, "key": kl, "value": f, "raw": val})
    return out


def _extract_next_data(html: str):
    m = re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html, re.DOTALL,
    )
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _extract_jsonld(html: str) -> list[dict]:
    blocks = re.findall(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
        html, re.DOTALL,
    )
    found = []
    for b in blocks:
        try:
            obj = json.loads(b)
        except json.JSONDecodeError:
            continue
        for node in (obj if isinstance(obj, list) else [obj]):
            offers = node.get("offers") if isinstance(node, dict) else None
            for off in (offers if isinstance(offers, list) else [offers]):
                if isinstance(off, dict) and "price" in off:
                    f = _to_float(off["price"])
                    if f:
                        found.append({"path": "jsonld.offers.price",
                                      "key": "price", "value": f, "raw": off["price"]})
    return found


def _regex_prices(html: str) -> list[float]:
    """Catch the split-cents render: '$4,099' followed by a stray '00'."""
    prices = []
    # Standard $X,XXX.XX
    for m in re.finditer(r"\$\s*([\d,]+\.\d{2})", html):
        prices.append(float(m.group(1).replace(",", "")))
    # Split cents: $4,099</...>00  -> reconstruct
    for m in re.finditer(r"\$\s*([\d,]+)\D{0,40}?>(\d{2})<", html):
        dollars = m.group(1).replace(",", "")
        if dollars.isdigit():
            prices.append(float(f"{dollars}.{m.group(2)}"))
    return [p for p in prices if p >= 50]


def extract(html: str, variant_id: str | None, verbose: bool = False) -> dict:
    """Return {current, was, on_sale, source, candidates}."""
    candidates: list[dict] = []
    source = None

    nd = _extract_next_data(html)
    if nd is not None:
        candidates = _collect_prices(nd)
        if candidates:
            source = "__NEXT_DATA__"

    if not candidates:
        jl = _extract_jsonld(html)
        if jl:
            candidates = jl
            source = "json-ld"

    current = was = None

    if candidates:
        # Prefer candidates inside the subtree of the matching variant id.
        scoped = []
        if variant_id and nd is not None:
            prefixes = _variant_prefixes(nd, variant_id)
            for c in candidates:
                if any(c["path"] == pre or c["path"].startswith(pre + ".")
                       or c["path"].startswith(pre + "[")
                       for pre in prefixes):
                    scoped.append(c)
        pool = scoped or candidates

        def pick(keys):
            for k in keys:
                hits = sorted((c for c in pool if c["key"] == k or c["key"].endswith(k)),
                              key=lambda c: c["value"])
                if hits:
                    return hits[0]["value"]
            return None

        current = pick(CURRENT_KEYS)
        was = pick(WAS_KEYS)
        # If "was" isn't strictly higher, it's not a real strikethrough.
        if was is not None and current is not None and was <= current:
            was = None

    if current is None:
        rx = sorted(set(_regex_prices(html)))
        if rx:
            source = source or "regex"
            current = rx[0]
            if len(rx) > 1 and rx[-1] > rx[0]:
                was = rx[-1]

    return {
        "current": current,
        "was": was,
        "on_sale": bool(was and current and was > current),
        "source": source,
        "candidates": candidates if verbose else None,
    }


# ----------------------------------------------------------------------------- state


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_state(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n")


# ----------------------------------------------------------------------------- alerts


def should_alert(product: dict, prev: dict | None, cur: dict) -> str | None:
    """Return an alert message, or None."""
    price = cur["current"]
    if price is None:
        return None
    mode = product.get("notify_on", "any_drop")
    name = product["name"]

    if mode == "below_target":
        target = product.get("target_price")
        if target is not None and price <= target:
            return f"💰 {name} is ${price:,.2f} — at or below your ${target:,.2f} target."
        return None

    if mode == "on_sale":
        if cur["on_sale"]:
            return (f"🏷️ {name} is on sale: ${price:,.2f} "
                    f"(was ${cur['was']:,.2f}).")
        return None

    # default: any_drop vs last-seen price
    if prev and prev.get("current") is not None and price < prev["current"]:
        return (f"📉 {name} dropped from ${prev['current']:,.2f} "
                f"to ${price:,.2f}.")
    return None


def notify_webhook(message: str):
    url = os.environ.get("ALERT_WEBHOOK_URL")
    if not url:
        return
    # Slack and Discord both accept a JSON body; Slack uses "text",
    # Discord uses "content". Send both keys to cover either.
    payload = json.dumps({"text": message, "content": message}).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:  # noqa: BLE001 - best effort
        print(f"  webhook failed: {e}", file=sys.stderr)


# ----------------------------------------------------------------------------- main


def run(config_path: Path, state_path: Path, dump: bool) -> int:
    config = json.loads(config_path.read_text())
    products = config["products"]
    state = load_state(state_path)
    alerts: list[str] = []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for p in products:
        name = p["name"]
        print(f"→ {name}")
        try:
            html = fetch(p["url"])
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR fetching: {e}", file=sys.stderr)
            continue

        if dump:
            Path(f"dump_{re.sub(r'[^a-z0-9]+', '_', name.lower())}.html").write_text(html)

        cur = extract(html, p.get("variant_id"), verbose=dump)
        if cur["current"] is None:
            print("  ERROR: no price found "
                  "(run with --dump and inspect candidates)", file=sys.stderr)
            if dump and cur["candidates"] is not None:
                for c in cur["candidates"][:40]:
                    print(f"    {c['path']} = {c['raw']} -> {c['value']}")
            continue

        tag = " ON SALE" if cur["on_sale"] else ""
        was = f" (was ${cur['was']:,.2f})" if cur["was"] else ""
        print(f"  ${cur['current']:,.2f}{was}{tag}  [via {cur['source']}]")

        prev = state.get(p["url"])
        msg = should_alert(p, prev, cur)
        if msg:
            print(f"  ALERT: {msg}")
            alerts.append(msg)

        history = (prev or {}).get("history", [])
        if not prev or prev.get("current") != cur["current"]:
            history = (history + [{"t": now, "price": cur["current"]}])[-200:]
        state[p["url"]] = {
            "name": name,
            "current": cur["current"],
            "was": cur["was"],
            "on_sale": cur["on_sale"],
            "checked": now,
            "history": history,
        }

    save_state(state_path, state)

    # Hand off to the workflow / webhook.
    if alerts:
        body = "\n".join(f"- {a}" for a in alerts)
        body += f"\n\nChecked {now}."
        Path("alert.md").write_text(body + "\n")
        for a in alerts:
            notify_webhook(a)
        _gh_output("has_alert", "true")
        _step_summary("## 🔔 Price alert\n\n" + body)
    else:
        _gh_output("has_alert", "false")
        _step_summary("No alerts this run.")
    return 0


def _gh_output(key: str, val: str):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{key}={val}\n")


def _step_summary(md: str):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(md + "\n")


# ----------------------------------------------------------------------------- selftest


FIXTURE = '''
<html><body>
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"product":{"id":"5637491100","variants":[
  {"id":"5637491242","size":"King","listPrice":4099.00,"salePrice":3499.00},
  {"id":"5637491240","size":"Queen","listPrice":3699.00,"salePrice":3699.00}
]}}}}
</script>
<div class="price">$3,499<sup>00</sup></div>
</body></html>
'''


def selftest() -> int:
    print("running selftest...")
    r = extract(FIXTURE, "5637491242", verbose=True)
    assert r["current"] == 3499.0, r
    assert r["was"] == 4099.0, r
    assert r["on_sale"] is True, r
    assert r["source"] == "__NEXT_DATA__", r
    # variant scoping: queen should not leak in
    r2 = extract(FIXTURE, "5637491240", verbose=False)
    assert r2["current"] == 3699.0, r2
    assert r2["on_sale"] is False, r2
    # regex split-cents fallback
    rx = _regex_prices('<div>$3,499</sup>00<')
    assert 3499.0 in rx, rx
    print("OK — all selftests passed")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Mattress Firm price watcher")
    ap.add_argument("--config", default="config.json", type=Path)
    ap.add_argument("--state", default="data/price_history.json", type=Path)
    ap.add_argument("--dump", action="store_true",
                    help="save fetched HTML + print all price candidates")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        sys.exit(selftest())
    sys.exit(run(args.config, args.state, args.dump))


if __name__ == "__main__":
    main()
