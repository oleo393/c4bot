"""Automatic e-mail-code login for donutflip.net (used by c4bot.py; also runnable on your PC to test one account).

Flow (selectors taken from the site's own HTML):
  header "Sign In" -> dialog -> "Email" tab -> type the address -> tick the Terms box -> "Send Code"
  -> read the 6-digit code from the Yahoo mailbox over IMAP (app password) -> type it -> "Sign In".

The code mail is found by its subject ("123456 is your DonutFlip email code") and by the account's address
(the forwarded mail has it in the From / To / Duck-Original-To headers). Every poll opens a fresh IMAP
connection, so new mail is seen without any 'refresh'.

Local test (headed Chrome, saves a plain storage_state file, uploads nothing):
  python autologin.py --email jack-app-sky@duck.com --login-json login.json --out storage_state_main-1.json
login.json: {"imap_host": "imap.mail.yahoo.com", "imap_user": "you@yahoo.com", "imap_password": "<yahoo app password>"}
"""
import argparse, email, imaplib, json, os, random, re, sys, time
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

import requests

HOME_URL = "https://donutflip.net/"
SIGN_IN = "header.site-navbar button:text-is('Sign In')"
BALANCE = "header.site-navbar button.nav-coin-pill > span"
DIALOG = "div[role='dialog'][aria-labelledby='signup-flow-title']"
SUBJECT_RE = re.compile(r"(\d{6})\s+is your DonutFlip email code", re.I)
FOLDERS = ("INBOX", "Bulk Mail", "Bulk", "Spam")      # Yahoo's junk folder is called "Bulk" / "Bulk Mail"
DATE_MARGIN_S = 10                                    # mail Date may be slightly before our click (clock skew)


class LoginError(Exception):
    pass


# ---------------------------------------------------------------- credentials / discord
def load_creds(store_root=None, path=None):
    """imap_user / imap_password / imap_host / login_webhook / enabled from login.json (private repo root or a local
    file) with environment overrides C4_IMAP_USER, C4_IMAP_PASSWORD, C4_IMAP_HOST, C4_LOGIN_WEBHOOK."""
    data = {}
    try:
        if path and os.path.exists(path):
            data = json.load(open(path, encoding="utf-8-sig"))
        elif store_root is not None and store_root.enabled:
            blob = store_root.get("login.json")
            data = json.loads(blob.decode("utf-8-sig")) if blob else {}
    except Exception as e:
        print("login.json could not be read:", type(e).__name__, flush=True)
    for key, env in (("imap_user", "C4_IMAP_USER"), ("imap_password", "C4_IMAP_PASSWORD"),
                     ("imap_host", "C4_IMAP_HOST"), ("login_webhook", "C4_LOGIN_WEBHOOK")):
        if os.environ.get(env, "").strip():
            data[key] = os.environ[env].strip()
    return data


def post_discord(url, text, screenshot=None):
    payload = {"content": text[:1900], "allowed_mentions": {"parse": []}}
    try:
        for _ in range(3):
            if screenshot:
                r = requests.post(url, data={"payload_json": json.dumps(payload)},
                                  files={"file": ("login.png", screenshot, "image/png")}, timeout=20)
            else:
                r = requests.post(url, json=payload, timeout=20)
            if r.status_code == 429:
                time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
                continue
            return r.ok
    except Exception as e:
        print("Discord send failed:", type(e).__name__, flush=True)
    return False


# ---------------------------------------------------------------- mailbox
def _h(value):
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return value or ""


def address_pattern(addr):
    """Matches the address as a whole (not as the tail of a longer alias such as 'jack-app-sky@duck.com' for 'sky@duck.com')."""
    return re.compile(r"(?<![a-z0-9.\-])" + re.escape(addr.strip().lower()))


def match_code(msg, pat, since_utc, exclude=()):
    """-> (code, mail date) if this message is a DonutFlip code mail for our address that is not older than `since_utc`."""
    m = SUBJECT_RE.search(_h(msg.get("Subject")))
    if not m or m.group(1) in exclude:
        return None
    blob = " ".join(_h(msg.get(k)) for k in ("From", "To", "Duck-Original-To", "Delivered-To", "X-Original-To")).lower()
    if not pat.search(blob):
        return None
    dt = None
    try:
        dt = parsedate_to_datetime(msg.get("Date"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt < since_utc - timedelta(seconds=DATE_MARGIN_S):
            return None
    except Exception:
        pass   # unreadable Date: accept it, the newest mail is checked first
    return m.group(1), dt


def _scan_mailbox(creds, pat, since_utc, exclude):
    M = imaplib.IMAP4_SSL(creds.get("imap_host") or "imap.mail.yahoo.com", 993, timeout=30)
    try:
        M.login(creds["imap_user"], creds["imap_password"])
        for folder in FOLDERS:
            typ, _ = M.select(f'"{folder}"', readonly=True)
            if typ != "OK":
                continue
            typ, data = M.search(None, "SINCE", (since_utc - timedelta(days=1)).strftime("%d-%b-%Y"))
            if typ != "OK" or not data or not data[0]:
                continue
            for mid in reversed(data[0].split()[-25:]):     # newest first
                typ, md = M.fetch(mid, "(BODY.PEEK[HEADER])")
                if typ != "OK" or not md or not isinstance(md[0], tuple):
                    continue
                hit = match_code(email.message_from_bytes(md[0][1]), pat, since_utc, exclude)
                if hit:
                    return hit[0]
        return None
    finally:
        try:
            M.logout()
        except Exception:
            pass


def fetch_code(creds, addr, since_ts, timeout=150, exclude=(), interval=4, log=print):
    """Wait for the code mail sent after `since_ts` (epoch seconds) to `addr`. Raises LoginError on timeout / bad IMAP login."""
    since = datetime.fromtimestamp(since_ts, timezone.utc)
    pat = address_pattern(addr)
    end, tries, last = time.monotonic() + timeout, 0, None
    while time.monotonic() < end:
        tries += 1
        try:
            code = _scan_mailbox(creds, pat, since, exclude)
            if code:
                log(f"code mail found after {tries} poll(s)")
                return code
        except imaplib.IMAP4.error as e:
            if "AUTHENTICATIONFAILED" in str(e).upper() or "LOGIN" in str(e).upper():
                raise LoginError("IMAP login failed: check imap_user and the Yahoo APP password")
            last = e
        except Exception as e:
            last = e
        time.sleep(interval)
    raise LoginError(f"no code mail for this address within {timeout}s" + (f" (last mailbox error: {type(last).__name__})" if last else ""))


# ---------------------------------------------------------------- browser flow
def _title(page):
    try:
        return page.title()
    except Exception:
        return ""


def _is_challenge(page):
    t = _title(page).lower()
    return any(k in t for k in ("just a moment", "attention required", "cloudflare"))


def logged_in(page):
    try:
        return page.locator(BALANCE).first.is_visible() and page.locator(SIGN_IN).count() == 0
    except Exception:
        return False


def _wait_enabled(page, loc, seconds, what):
    for _ in range(int(seconds * 5)):
        try:
            if loc.is_enabled():
                return
        except Exception:
            pass
        page.wait_for_timeout(200)
    raise LoginError(f"{what} stayed disabled")


def login_flow(page, addr, creds, used_codes=None, log=print):
    """Log in on `page`. True when the header shows the balance. Raises LoginError (or Playwright errors) otherwise."""
    used_codes = used_codes if used_codes is not None else set()
    page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
    end = time.monotonic() + 45
    while time.monotonic() < end and _is_challenge(page):
        page.wait_for_timeout(1000)
    if _is_challenge(page):
        raise LoginError(f"Cloudflare challenge did not clear (title '{_title(page)}')")
    page.wait_for_selector(f"{BALANCE}, {SIGN_IN}", timeout=25000)
    if logged_in(page):
        log("already logged in")
        return True

    page.locator(SIGN_IN).first.click(timeout=10000)
    dlg = page.locator(DIALOG)
    dlg.wait_for(state="visible", timeout=15000)
    log("sign-in dialog open")

    dlg.locator("button[role='tab']:has-text('Email')").first.click(timeout=8000)
    inp = dlg.locator("input[aria-label='Email address']")
    inp.wait_for(state="visible", timeout=10000)
    inp.click()
    page.keyboard.press("Control+A")
    page.keyboard.type(addr, delay=random.randint(35, 80))

    box = dlg.locator("input[type='checkbox']").first
    try:
        if not box.is_checked():
            box.check(force=True, timeout=4000)
    except Exception:
        pass
    if not box.is_checked():     # custom-styled box: click the left edge of its label (away from the Terms link)
        dlg.locator("label:has(input[type='checkbox'])").first.click(position={"x": 6, "y": 8}, timeout=4000)
    if not box.is_checked():
        raise LoginError("could not tick the Terms checkbox")

    send = dlg.locator("button[type='submit']:has-text('Send Code')").first
    _wait_enabled(page, send, 10, "'Send Code'")
    t_send = time.time()
    send.click()
    log("code requested")

    code_inp = dlg.locator("input[aria-label='Email code']")
    code_inp.wait_for(state="visible", timeout=25000)
    code = fetch_code(creds, addr, t_send, exclude=used_codes, log=log)
    used_codes.add(code)
    code_inp.click()
    page.keyboard.type(code, delay=random.randint(50, 110))

    sign = dlg.locator("button[type='submit']:has-text('Sign In')").first
    _wait_enabled(page, sign, 10, "'Sign In' (after the code)")
    sign.click()
    end = time.monotonic() + 40
    while time.monotonic() < end:
        if logged_in(page):
            log("logged in")
            return True
        page.wait_for_timeout(500)
    raise LoginError("code submitted but the header never showed the balance (wrong or expired code?)")


# ---------------------------------------------------------------- local test
def _cli():
    ap = argparse.ArgumentParser(description="Test the automatic login for one account on this PC.")
    ap.add_argument("--email", required=True, help="the address this DonutFlip account uses (e.g. the duck.com alias)")
    ap.add_argument("--login-json", default="login.json")
    ap.add_argument("--out", default="storage_state_test.json")
    ap.add_argument("--headless", action="store_true")
    a = ap.parse_args()
    creds = load_creds(path=a.login_json)
    if not creds.get("imap_user") or not creds.get("imap_password"):
        sys.exit("login.json needs imap_user and imap_password (Yahoo app password).")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=a.headless, ignore_default_args=["--enable-automation"],
                                    args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(viewport={"width": 1366, "height": 800})
        page = ctx.new_page()
        try:
            login_flow(page, a.email, creds, log=lambda m: print("[login]", m, flush=True))
            ctx.storage_state(path=a.out)
            print("OK: logged in, saved", a.out)
        except Exception as e:
            page.screenshot(path="login_failed.png")
            print("FAILED:", type(e).__name__, e, "(screenshot: login_failed.png)")
        finally:
            browser.close()


if __name__ == "__main__":
    _cli()