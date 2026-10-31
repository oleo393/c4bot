"""Balance report -> the 'balance_webhook': one line per account (balance, preset, difference, change since the last
hour) plus an OVERALL line. Reads the last balance each session saved (stats*.enc) from the PRIVATE data repo.

  python balance.py          send ONE report now (this is what the hourly cron-job.org call runs)
  python balance.py --loop   (optional) stay alive and send a report every `balance_every_min` minutes

summary.json (private repo):
  "balance_webhook": "https://discord.com/api/webhooks/...",
  "presets": {"main": "500M", "alt": "200M", ...}      # target balance per account (K / M / B / T suffixes work)
  "balance_every_min": 60                               # only used by --loop
difference = balance - preset   (+ = above the target, - = below it)
chg        = balance now - balance in the report about an hour ago (every report is remembered in
             balance_state.json in the private repo; a manual extra run does not disturb the hourly comparison)
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

from store import Store
from summary import fmt, fmt_signed, load_json, load_stats, send, sessions_of, table

MULT = {"": 1, "k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}
HISTORY_KEEP_S = 3 * 24 * 3600
MIN_COMPARE_AGE_MIN = 20          # a snapshot younger than this (e.g. a manual re-run) is not used for "change"


def parse_amount(v):
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"(\d[\d,]*\.?\d*)\s*([kmbt]?)\b", str(v or "").strip(), re.I)
    return float(m.group(1).replace(",", "")) * MULT[m.group(2).lower()] if m else None


def age_min(iso):
    try:
        t = datetime.fromisoformat(iso)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return int((datetime.now(timezone.utc) - t).total_seconds() // 60)
    except Exception:
        return None


def seconds_until_due(last_sent, every_min, now, tolerance_s=120):
    """How long to wait before the next report is due (<= 0: send now)."""
    return float(last_sent or 0) + every_min * 60 - tolerance_s - now


def pick_previous(history, now, target_min=60):
    """The remembered report closest to one hour ago (but at least MIN_COMPARE_AGE_MIN old) -> (age in minutes, snapshot)."""
    best = None
    for h in history or []:
        try:
            age = (now - float(h["t"])) / 60
        except (KeyError, TypeError, ValueError):
            continue
        if age < MIN_COMPARE_AGE_MIN:
            continue
        if best is None or abs(age - target_min) < abs(best[0] - target_min):
            best = (age, h)
    return best


def age_label(minutes):
    m = int(round(minutes))
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def report(root, g):
    webhook = str(g.get("balance_webhook") or "").strip().strip("\"'")
    if not webhook.startswith("https://"):
        raise SystemExit("summary.json: 'balance_webhook' is empty or not a https:// Discord webhook URL.")
    presets = {k: parse_amount(v) for k, v in (g.get("presets") or {}).items()}
    accounts = g.get("accounts") or Store(prefix="accounts").list_dirs()

    state = load_json(root, "balance_state.json", {})
    now = time.time()
    prev = pick_previous(state.get("history"), now)
    prev_bal = (prev[1].get("bal") or {}) if prev else {}

    rows, missing, snapshot = [], [], {}
    tot_bal = tot_pre = tot_chg = 0.0
    any_chg = False
    oldest = ("", "")
    for acc in accounts:
        bal, at = None, ""
        for _, fname in sessions_of(acc):
            try:
                st = load_stats(acc, fname)
            except Exception as e:
                print("stats load failed for", acc, type(e).__name__)
                continue
            if st.get("balance") is not None and (st.get("balance_at") or "") >= at:
                bal, at = st["balance"], st.get("balance_at") or ""
        pre = presets.get(acc)
        if bal is None:
            rows.append([acc, "n/a", fmt(pre) if pre else "-", "n/a", "n/a"])
            missing.append(acc)
            continue
        snapshot[acc] = bal
        if not oldest[0] or at < oldest[0]:
            oldest = (at, acc)
        old = prev_bal.get(acc)
        chg = fmt_signed(bal - old) if old is not None else "-"
        if pre is None:
            rows.append([acc, fmt(bal), "-", "-", chg])
            continue
        rows.append([acc, fmt(bal), fmt(pre), fmt_signed(bal - pre), chg])
        tot_bal += bal
        tot_pre += pre
        if old is not None:
            tot_chg += bal - old
            any_chg = True
    if not rows:
        raise SystemExit("No accounts found.")
    rows.append(["OVERALL", fmt(tot_bal), fmt(tot_pre), fmt_signed(tot_bal - tot_pre), fmt_signed(tot_chg) if any_chg else "-"])

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    msg = f"\U0001F4B0 **Balances** \u00b7 {stamp}\n```\n" + table(rows, ["account", "balance", "preset", "diff", "chg"]) + "\n```"
    notes = []
    if prev:
        notes.append(f"chg = change since the report {age_label(prev[0])} ago")
    else:
        notes.append("chg: no earlier report to compare with yet (the first one appears with the next report)")
    mins = age_min(oldest[0]) if oldest[0] else None
    if mins is not None and mins >= 60:   # balances only change when a game ends, so say how old the oldest one is
        notes.append(f"oldest reading: {oldest[1]} ({mins // 60}h{mins % 60:02d}m ago, taken after its last finished game)")
    if missing:
        notes.append("no balance yet: " + ", ".join(missing))
    nopre = [a for a in accounts if presets.get(a) is None]
    if nopre:
        notes.append("no preset (not in OVERALL): " + ", ".join(nopre))
    msg += "\n" + "\n".join(notes)
    send(webhook, msg)
    print("Balance report sent", flush=True)

    # remember this report (for the next hour's comparison) and when it was sent (for --loop)
    history = [h for h in (state.get("history") or []) if now - float(h.get("t", 0)) < HISTORY_KEEP_S]
    history.append({"t": now, "bal": snapshot})
    state.update(last=now, history=history[-80:])
    try:
        root.get("balance_state.json")        # refresh the file's sha
        root.put("balance_state.json", (json.dumps(state) + "\n").encode(), "balance report")
    except Exception as e:
        print("could not save balance_state.json:", type(e).__name__, flush=True)


def main():
    root = Store(prefix="")
    if not root.enabled:
        sys.exit("C4_DATA_REPO / C4_DATA_TOKEN not set")
    if "--loop" not in sys.argv:
        report(root, load_json(root, "summary.json", {}))
        return
    deadline = time.monotonic() + float(os.environ.get("C4_MAX_RUNTIME_MIN", "340")) * 60
    while True:
        try:
            g = load_json(root, "summary.json", {})
            every = float(g.get("balance_every_min") or 60)
            state = load_json(root, "balance_state.json", {})
            wait = seconds_until_due(state.get("last"), every, time.time())
            if wait <= 0:
                report(root, g)
                wait = every * 60 - 120
        except SystemExit as e:
            print("balance report not sent:", e, flush=True)
            wait = 300
        except Exception as e:
            print("balance loop error:", type(e).__name__, flush=True)
            wait = 120
        wait = max(30.0, min(wait, 300.0))
        if time.monotonic() + wait > deadline:
            print("Loop time is over; the next scheduled run takes over.", flush=True)
            return
        time.sleep(wait)


if __name__ == "__main__":
    main()
