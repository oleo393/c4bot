"""Hourly balance check -> the 'balance_webhook'. One line per account: balance, preset (target) and difference,
plus an OVERALL line. Reads the last balance each session saved (stats*.enc) from the PRIVATE data repo.

summary.json (private repo):
  "balance_webhook": "https://discord.com/api/webhooks/...",
  "presets": {"main": "500M", "alt": "200M", ...}       # target balance per account (K / M / B / T suffixes work)
difference = balance - preset   (+ = above the target, - = below it)
"""
import re, sys
from datetime import datetime, timezone

from store import Store
from summary import fmt, fmt_signed, load_json, load_stats, send, sessions_of, table

MULT = {"": 1, "k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}


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


def main():
    root = Store(prefix="")
    if not root.enabled:
        sys.exit("C4_DATA_REPO / C4_DATA_TOKEN not set")
    g = load_json(root, "summary.json", {})
    webhook = str(g.get("balance_webhook") or "").strip().strip("\"'")
    if not webhook.startswith("https://"):
        sys.exit("summary.json: 'balance_webhook' is empty or not a https:// Discord webhook URL.")
    presets = {k: parse_amount(v) for k, v in (g.get("presets") or {}).items()}
    accounts = g.get("accounts") or Store(prefix="accounts").list_dirs()

    rows, tot_bal, tot_pre, missing, stale = [], 0.0, 0.0, [], []
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
            rows.append([acc, "n/a", fmt(pre) if pre else "-", "n/a"])
            missing.append(acc)
            continue
        if not oldest[0] or at < oldest[0]:
            oldest = (at, acc)
        if pre is None:
            rows.append([acc, fmt(bal), "-", "-"])
            continue
        rows.append([acc, fmt(bal), fmt(pre), fmt_signed(bal - pre)])
        tot_bal += bal
        tot_pre += pre
    if not rows:
        sys.exit("No accounts found.")
    rows.append(["OVERALL", fmt(tot_bal), fmt(tot_pre), fmt_signed(tot_bal - tot_pre)])

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    msg = f"\U0001F4B0 **Balances** \u00b7 {now}\n```\n" + table(rows, ["account", "balance", "preset", "diff"]) + "\n```"
    notes = []
    mins = age_min(oldest[0]) if oldest[0] else None
    if mins is not None and mins >= 60:   # balances only change when a game ends, so say how old the oldest one is
        notes.append(f"oldest reading: {oldest[1]} ({mins // 60}h{mins % 60:02d}m ago, taken after its last finished game)")
    if missing:
        notes.append("no balance yet: " + ", ".join(missing))
    nopre = [a for a in accounts if presets.get(a) is None]
    if nopre:
        notes.append("no preset (not in OVERALL): " + ", ".join(nopre))
    if notes:
        msg += "\n" + "\n".join(notes)
    send(webhook, msg)
    print("Balance report sent")


if __name__ == "__main__":
    main()
