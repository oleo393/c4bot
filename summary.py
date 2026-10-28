"""Daily report for all accounts -> the summary webhook. Runs hourly from daily-summary.yml, but sends only
once per day: at/after `summary_hour` (local time of `timezone`) and only if that day was not reported yet.

Reads from the PRIVATE data repo:  summary.json, accounts/<name>/stats.enc (every folder under accounts/, every stats*.enc inside it = one session), summary_state.json (written here).
Usage:  python summary.py            normal (scheduled) run
        python summary.py --force    send now for yesterday, ignore the once-a-day guard (does not mark it as sent)
        python summary.py --date 2026-10-26   report that day (implies --force)
"""
import json, os, re, sys, time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from store import Store

ARGS = sys.argv[1:]
FORCE = "--force" in ARGS or "--date" in ARGS
DATE_ARG = ARGS[ARGS.index("--date") + 1] if "--date" in ARGS else None


def fmt(n):
    sign, n = ("-" if n < 0 else ""), abs(n)
    for div, s in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            return f"{sign}{n / div:.2f}{s}"
    return f"{sign}{n:.0f}"


def fmt_signed(n):
    return ("+" if n > 0 else "") + fmt(n)


def unseal(blob):
    key = os.environ.get("STATE_KEY", "").strip()
    if key:
        from cryptography.fernet import Fernet
        try:
            return Fernet(key.encode()).decrypt(blob)
        except Exception:
            pass
    return blob


def load_json(store, path, default):
    blob = store.get(path)
    if blob is None:
        return default
    return json.loads(blob.decode("utf-8-sig"))


def sessions_of(account):
    """[(session name, stats file)] for accounts/<account>/: stats.enc -> 'main', stats-1.enc -> 'main-1' ..."""
    out = []
    for fn in Store(prefix=f"accounts/{account}").list_files():
        m = re.fullmatch(r"stats(?:-(\d+))?\.enc", fn)
        if m:
            out.append((account + (f"-{m.group(1)}" if m.group(1) else ""), fn))
    return out


def load_stats(account, fname):
    blob = Store(prefix=f"accounts/{account}").get(fname)
    return json.loads(unseal(blob)) if blob else {}


def day_totals(stats, day, tz):
    t = {"pnl": 0, "g": 0, "w": 0, "l": 0, "d": 0, "v": 0, "u": 0}
    for k, b in (stats.get("hourly") or {}).items():
        try:
            local = datetime.strptime(k, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc).astimezone(tz)
        except ValueError:
            continue
        if local.date().isoformat() == day:
            for f in t:
                t[f] += b.get(f, 0)
    return t


def table(rows, header):
    widths = [max(len(str(r[i])) for r in [header] + rows) for i in range(len(header))]
    line = lambda r: "  ".join(str(c).ljust(widths[0]) if i == 0 else str(c).rjust(widths[i]) for i, c in enumerate(r))
    return "\n".join([line(header)] + [line(r) for r in rows])


def send(webhook, text):
    for _ in range(3):
        r = requests.post(webhook, json={"content": text[:1990], "allowed_mentions": {"parse": []}}, timeout=20)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        r.raise_for_status()
        return
    raise RuntimeError("Discord rate limit")


def main():
    root = Store(prefix="")
    if not root.enabled:
        sys.exit("C4_DATA_REPO / C4_DATA_TOKEN not set")
    g = load_json(root, "summary.json", {})
    accounts = g.get("accounts") or Store(prefix="accounts").list_dirs()   # optional override; default = every folder
    webhook = str(g.get("summary_webhook") or "").strip().strip("\"'")
    if not accounts or not webhook.startswith("https://"):
        sys.exit("summary.json needs a valid 'summary_webhook' and at least one folder under accounts/")
    tz = ZoneInfo(g.get("timezone") or "UTC")
    hour = int(g.get("summary_hour") or 0)
    now = datetime.now(tz)

    state = load_json(root, "summary_state.json", {})
    day = DATE_ARG or (now - timedelta(days=1)).date().isoformat()
    if not FORCE:
        if now.hour < hour:
            print("Too early for today's report.")
            return
        if state.get("last_sent") == day:
            print("Already sent the report for", day)
            return

    rows_day, rows_all, tot_d = [], [], {"pnl": 0, "g": 0, "w": 0, "l": 0, "d": 0}
    tot_all = {"pnl": 0, "w": 0, "l": 0, "d": 0}
    acct_bal, any_bal = {}, False
    sessions = [x for acc in accounts for x in sessions_of(acc)]
    for name, (acc, fname) in [(n, (n.split("-")[0], f)) for n, f in sessions]:
        try:
            st = load_stats(acc, fname)
        except Exception as e:
            print("stats load failed for", name, type(e).__name__)
            st = {}
        d = day_totals(st, day, tz)
        rec = st.get("rec") or {}
        bal = st.get("balance")
        rows_day.append([name, fmt_signed(d["pnl"]), d["w"], d["l"], d["d"], d["g"], fmt(bal) if bal is not None else "n/a"])
        rows_all.append([name, fmt_signed(st.get("pnl", 0)), rec.get("w", 0), rec.get("l", 0), rec.get("d", 0)])
        for f in tot_d:
            tot_d[f] += d[f]
        tot_all["pnl"] += st.get("pnl", 0)
        for f in ("w", "l", "d"):
            tot_all[f] += rec.get(f, 0)
        if bal is not None:   # sessions of one account share ONE balance: keep the most recent reading per account
            any_bal = True
            at = st.get("balance_at") or ""
            if acc not in acct_bal or at >= acct_bal[acc][0]:
                acct_bal[acc] = (at, bal)
    tot_bal = sum(v for _, v in acct_bal.values())
    rows_day.append(["TOTAL", fmt_signed(tot_d["pnl"]), tot_d["w"], tot_d["l"], tot_d["d"], tot_d["g"], fmt(tot_bal) if any_bal else "n/a"])
    rows_all.append(["TOTAL", fmt_signed(tot_all["pnl"]), tot_all["w"], tot_all["l"], tot_all["d"]])

    send(webhook, f"\U0001F4CA **Daily report** · {day} ({g.get('timezone') or 'UTC'})\n"
                  "```\n" + table(rows_day, ["", "P/L", "W", "L", "D", "games", "balance"]) + "\n```")
    send(webhook, "**All-time** (since last P/L reset)\n"
                  "```\n" + table(rows_all, ["", "P/L", "W", "L", "D"]) + "\n```")
    print("Report sent for", day)
    if not FORCE:
        state["last_sent"] = day
        root.get("summary_state.json")   # refresh sha
        root.put("summary_state.json", (json.dumps(state) + "\n").encode(), "daily report sent")


if __name__ == "__main__":
    main()
