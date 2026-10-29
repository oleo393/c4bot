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


def session_order(name):
    """main < main-1 < main-2"""
    base, _, n = name.partition("-")
    return (base, int(n) if n.isdigit() else 0)


def blank():
    return {"pnl": 0, "w": 0, "l": 0, "d": 0, "v": 0, "u": 0, "g": 0}


def add(a, b):
    for k in a:
        a[k] += b.get(k, 0)
    return a


def send_table(webhook, title, header, groups):
    """groups = [[row, row, ...], ...] (an account row followed by its session rows). Splits into several messages
    at group borders if one Discord message (2000 characters) would not hold everything."""
    chunks, cur = [], []
    for grp in groups:
        trial = cur + grp
        if len(table(trial, header)) > 1700 and cur:
            chunks.append(cur)
            cur = list(grp)
        else:
            cur = trial
    if cur:
        chunks.append(cur)
    for i, ch in enumerate(chunks):
        head = title if i == 0 else f"{title} (continued)"
        send(webhook, f"{head}\n```\n" + table(ch, header) + "\n```")


def main():
    root = Store(prefix="")
    if not root.enabled:
        sys.exit("C4_DATA_REPO / C4_DATA_TOKEN not set")
    g = load_json(root, "summary.json", {})
    webhook = str(g.get("summary_webhook") or "").strip().strip("\"'")
    if not webhook.startswith("https://"):
        sys.exit("summary.json: 'summary_webhook' is empty or not a https:// Discord webhook URL. Nothing can be sent.")
    accounts = g.get("accounts") or Store(prefix="accounts").list_dirs()   # optional override; default = every folder
    if not accounts:
        sys.exit("No account folders found under accounts/ in the private repo.")
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

    # ---- collect: per account -> per session
    data = {}
    for acc in accounts:
        sess = {}
        for name, fname in sessions_of(acc):
            try:
                st = load_stats(acc, fname)
            except Exception as e:
                print("stats load failed for", name, type(e).__name__)
                st = {}
            d = day_totals(st, day, tz)
            rec = st.get("rec") or {}
            sess[name] = {
                "day": d,
                "all": {"pnl": st.get("pnl", 0), "w": rec.get("w", 0), "l": rec.get("l", 0), "d": rec.get("d", 0),
                        "v": rec.get("v", 0), "u": rec.get("u", 0),
                        "g": sum(rec.get(k, 0) for k in ("w", "l", "d", "v", "u"))},
                "bal": st.get("balance"), "bal_at": st.get("balance_at") or ""}
        if sess:
            data[acc] = dict(sorted(sess.items(), key=lambda kv: session_order(kv[0])))
    if not data:
        sys.exit("No stats files (stats.enc / stats-N.enc) found yet. Nothing to report.")

    dh = ["", "P/L", "W", "L", "D", "V", "games", "balance"]
    ah = ["", "P/L", "W", "L", "D", "V"]
    day_groups, all_groups = [], []
    tot_day, tot_all, tot_bal, any_bal = blank(), blank(), 0, False
    for acc, sess in data.items():
        a_day, a_all = blank(), blank()
        bal, bal_at = None, ""
        for v in sess.values():
            add(a_day, v["day"])
            add(a_all, v["all"])
            if v["bal"] is not None and v["bal_at"] >= bal_at:   # sessions share ONE balance: latest reading wins
                bal, bal_at = v["bal"], v["bal_at"]
        add(tot_day, a_day)
        add(tot_all, a_all)
        if bal is not None:
            tot_bal += bal
            any_bal = True
        multi = len(sess) > 1
        label = f"{acc} (all)" if multi else acc
        row_d = lambda n, t, b="": [n, fmt_signed(t["pnl"]), t["w"], t["l"], t["d"], t["v"], t["g"], b]
        row_a = lambda n, t: [n, fmt_signed(t["pnl"]), t["w"], t["l"], t["d"], t["v"]]
        gd = [row_d(label, a_day, fmt(bal) if bal is not None else "n/a")]
        ga = [row_a(label, a_all)]
        if multi:   # breakdown per session
            for name, v in sess.items():
                gd.append(row_d("  " + name, v["day"]))
                ga.append(row_a("  " + name, v["all"]))
        day_groups.append(gd)
        all_groups.append(ga)
    day_groups.append([["OVERALL", fmt_signed(tot_day["pnl"]), tot_day["w"], tot_day["l"], tot_day["d"], tot_day["v"],
                        tot_day["g"], fmt(tot_bal) if any_bal else "n/a"]])
    all_groups.append([["OVERALL", fmt_signed(tot_all["pnl"]), tot_all["w"], tot_all["l"], tot_all["d"], tot_all["v"]]])

    send_table(webhook, f"\U0001F4CA **Daily report** \u00b7 {day} ({g.get('timezone') or 'UTC'})", dh, day_groups)
    send_table(webhook, "**All-time** (since the last P/L reset)", ah, all_groups)
    print("Report sent for", day)
    if not FORCE:
        state["last_sent"] = day
        root.get("summary_state.json")   # refresh sha
        root.put("summary_state.json", (json.dumps(state) + "\n").encode(), "daily report sent")


if __name__ == "__main__":
    main()
