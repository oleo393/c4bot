"""DonutFlip Connect 4 bot (create mode only), designed to run on GitHub Actions from a PUBLIC repo.

Nothing private lives in the public repo: config.json, state.enc and stats.enc are kept in a
separate private data repo (see store.py). config.json is re-read before every game.
Notifications: Discord webhook (DISCORD_WEBHOOK_URL secret or .secret file).
Solver: solver.js driven through solver_server.js (Node).
"""
import base64, hashlib, json, os, random, re, signal, subprocess, sys, threading, time
from collections import deque
from datetime import datetime
from pathlib import Path

import requests
from store import Store
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
HEADLESS = "--headed" not in sys.argv
LOBBY_URL = "https://donutflip.net/connect-four"
HOME_URL = "https://donutflip.net/"

STORE = Store()
IN_ACTIONS = bool(os.environ.get("GITHUB_ACTIONS"))
CONFIG_FILE = HERE / "config.json"         # local-dev fallback (gitignored)
STATE_ENC = HERE / "state.enc"             # local-dev fallback (gitignored)
STATE_FILE = HERE / "storage_state.json"   # decrypted copy, never committed
STATS_FILE = HERE / "stats.enc"            # local-dev fallback (gitignored)
SECRET_FILE = HERE / ".secret"
BOOKS_DIR = os.environ.get("C4_BOOKS_DIR", str(HERE / "books"))
RUN_START = time.monotonic()
MAX_RUNTIME_MIN = float(os.environ.get("C4_MAX_RUNTIME_MIN", "0") or 0)  # 0 = unlimited

DEFAULT_CFG = {
    "enabled": True,            # false = finish the current game, then stop
    "stake": "1000",            # bet per game: 1000, "1,000", "5k", "2.5m"
    "seat": "auto",             # "auto" | "creator" | "opponent" (force C / O)
    "player_id": "",            # fallback for seat detection (prefer the C4_PLAYER_ID secret)
    "max_loss": 0,              # stop when net P/L <= -max_loss (0 = off)
    "max_games": 0,             # stop after N games this run (0 = off)
    "min_balance": 0,           # stop if balance drops below this (0 = off)
    "notify_every": 1,          # Discord message every N games (0 = errors only)
    "reset_pnl_token": "",      # change this value to reset the P/L counters
    "opponent_wait_s": 1800,    # give up on an unjoined listing after this many seconds (0 = never)
    "safe_remaining_s": 2,      # play best-so-far when the turn timer is this low
    "min_delay_ms": 400,        # wait this long after a new position before clicking
    "cf_retry_minutes": [3, 5, 10, 20, 30, 60],  # waits between retries when Cloudflare blocks (last value repeats)
    "cf_give_up_min": 300,      # stop after being blocked this long in a row (the next hourly run tries again)
}

END_BUFFER_S = 5
PAYOUT_SETTLE_S = 5
CHALLENGE_WAIT = 45
LOGIN_WAIT = 30
MAX_CONSECUTIVE_ERRORS = 5
ERROR_BACKOFF = 120

SIGN_IN = "header.site-navbar button:text-is('Sign In')"
BALANCE = "header.site-navbar button.nav-coin-pill > span"
STAKE_INPUT = "#connect-four-stake"
CREATE_BTN = "button.pxHubq_primary:has-text('Create game')"
GAME_RE = re.compile(r"/connect-four/game/([^/?#]+)/?(?:[?#]|$)")
ORDER = [3, 2, 4, 1, 5, 0, 6]
W, H = 7, 6
SEATS = ("creator", "opponent")
other = lambda s: "opponent" if s == "creator" else "creator"


class BotError(Exception):
    def __init__(self, msg, shot=None, fatal=False, blocked=False):
        super().__init__(msg)
        self.shot, self.fatal, self.blocked = shot, fatal, blocked


# ---------------------------------------------------------------- helpers
MULT = {"": 1, "k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}


def parse_amount(text):
    if isinstance(text, (int, float)):
        return float(text)
    m = re.search(r"(\d[\d,]*\.?\d*)\s*([kmbt]?)\b", (text or "").strip(), re.I)
    if not m:
        return None
    return float(m.group(1).replace(",", "")) * MULT[m.group(2).lower()]


def fmt(n):
    sign, n = ("-" if n < 0 else ""), abs(n)
    for div, s in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            return f"{sign}{n / div:.2f}{s}"
    return f"{sign}{n:.0f}"


def fmt_signed(n):
    return ("+" if n > 0 else "") + fmt(n)


def now_str():
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S UTC%z")


def load_webhook():
    url = None
    if SECRET_FILE.exists():
        for line in SECRET_FILE.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                url = line.split("=", 1)[1].strip() if line.startswith("DISCORD_WEBHOOK_URL=") else line
                break
    url = (url or os.environ.get("DISCORD_WEBHOOK_URL") or "").strip().strip("\"'")
    return url or None


WEBHOOK = load_webhook()


def notify(msg, screenshot=None):
    print("[notify] message sent" if IN_ACTIONS else f"[notify] {msg}", flush=True)
    if not WEBHOOK:
        return
    payload = {"content": msg[:1900], "allowed_mentions": {"parse": []}}
    try:
        for _ in range(3):
            if screenshot:
                r = requests.post(WEBHOOK, data={"payload_json": json.dumps(payload)},
                                  files={"file": ("error.png", screenshot, "image/png")}, timeout=20)
            else:
                r = requests.post(WEBHOOK, json=payload, timeout=20)
            if r.status_code == 429:
                time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
                continue
            if not r.ok:
                print("Discord error:", r.status_code, r.text[:200])
            return
    except Exception as e:
        print("Discord send failed:", e)


def shot(page):
    try:
        return page.screenshot()
    except Exception:
        return None


def real_click(page, locator):
    locator.scroll_into_view_if_needed(timeout=10000)
    box = locator.bounding_box()
    if not box:
        raise BotError("Element has no bounding box", shot(page))
    x = box["x"] + box["width"] * random.uniform(0.35, 0.65)
    y = box["y"] + box["height"] * random.uniform(0.35, 0.65)
    page.mouse.move(x, y, steps=random.randint(8, 20))
    page.wait_for_timeout(random.randint(80, 250))
    page.mouse.click(x, y)


# ---------------------------------------------------------------- live config (the control surface)
_last_cfg = dict(DEFAULT_CFG)
_cfg_warned = False


def get_cfg():
    """config.json from the private data repo (edits apply live), else the local file."""
    global _last_cfg, _cfg_warned
    raw = None
    if STORE.enabled:
        try:
            data = STORE.get("config.json")
            if data is None:   # first run: create it so it can be edited on github.com
                seed = {k: v for k, v in DEFAULT_CFG.items() if k != "player_id"}
                STORE.put("config.json", (json.dumps(seed, indent=2) + "\n").encode(), "create config")
                data = json.dumps(seed).encode()
            raw = data.decode("utf-8-sig")
        except Exception as e:
            print("config fetch failed:", type(e).__name__, flush=True)
    if raw is None and CONFIG_FILE.exists():
        raw = CONFIG_FILE.read_text(encoding="utf-8-sig")
    if raw is not None:
        try:
            cfg = dict(DEFAULT_CFG)
            cfg.update({k: v for k, v in json.loads(raw).items() if not k.startswith("_")})
            if parse_amount(cfg["stake"]) in (None, 0):
                raise ValueError(f"bad stake {cfg['stake']!r}")
            _last_cfg, _cfg_warned = cfg, False
        except Exception as e:
            if not _cfg_warned:
                notify(f"⚠️ config.json is invalid ({e}). Keeping the last good settings.")
                _cfg_warned = True
    return _last_cfg


# ---------------------------------------------------------------- encrypted login state + stats
def _fernet():
    key = os.environ.get("STATE_KEY", "").strip()
    if not key:
        return None
    from cryptography.fernet import Fernet
    return Fernet(key.encode())


def _seal(data):
    f = _fernet()
    return f.encrypt(data) if f else data


def _unseal(blob):
    f = _fernet()
    if f:
        try:
            return f.decrypt(blob)
        except Exception:
            pass
    return blob


def load_stats():
    try:
        blob = STORE.get("stats.enc") if STORE.enabled else (STATS_FILE.read_bytes() if STATS_FILE.exists() else None)
        return json.loads(_unseal(blob)) if blob else {}
    except Exception as e:
        print("could not load stats:", type(e).__name__, flush=True)
        return {}


def save_stats(st):
    blob = _seal(json.dumps(st).encode())
    try:
        if STORE.enabled:
            STORE.put("stats.enc", blob, "stats")
        else:
            STATS_FILE.write_bytes(blob)
    except Exception as e:
        print("could not save stats:", type(e).__name__, flush=True)


def prepare_state():
    """Create storage_state.json from the private repo's state.enc (preferred), a local state.enc,
    or the STATE_JSON secret. Returns a hash of the source."""
    f = _fernet()
    blob = None
    if STORE.enabled:
        blob = STORE.get("state.enc")
    elif STATE_ENC.exists():
        blob = STATE_ENC.read_bytes()
    if blob:
        if not f:
            sys.exit("state.enc exists but STATE_KEY is not set.")
        STATE_FILE.write_bytes(f.decrypt(blob))
        return hashlib.sha256(blob).hexdigest()
    sec = os.environ.get("STATE_JSON", "").strip()
    if sec:
        if not sec.startswith("{"):
            sec = base64.b64decode(sec).decode("utf-8")
        json.loads(sec)  # validate
        STATE_FILE.write_text(sec, encoding="utf-8")
        return hashlib.sha256(sec.encode()).hexdigest()
    if STATE_FILE.exists():  # local use
        return hashlib.sha256(STATE_FILE.read_bytes()).hexdigest()
    sys.exit("No login state: put state.enc in the private data repo + STATE_KEY, or set the STATE_JSON secret (see README).")


def save_state(ctx):
    f = _fernet()
    if not f:
        return
    try:
        blob = f.encrypt(json.dumps(ctx.storage_state()).encode())
        if STORE.enabled:
            STORE.put("state.enc", blob, "session")
        else:
            STATE_ENC.write_bytes(blob)
    except Exception as e:
        print("could not save state:", type(e).__name__, flush=True)


# ---------------------------------------------------------------- solver (Node subprocess)
class Solver:
    def __init__(self):
        self.res, self.next_id, self.books = {}, 0, {}
        self.ready = threading.Event()
        self.lock = threading.Lock()
        self.p = subprocess.Popen(["node", str(HERE / "solver_server.js"), BOOKS_DIR, "24", "7000"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                  bufsize=1, cwd=str(HERE))
        threading.Thread(target=self._read, daemon=True).start()
        if not self.ready.wait(180):
            raise RuntimeError("solver did not start")

    def _read(self):
        for line in self.p.stdout:
            try:
                m = json.loads(line)
            except Exception:
                continue
            if m.get("ready"):
                self.books = m.get("books", {})
                self.ready.set()
                continue
            with self.lock:
                self.res.setdefault(m.get("id"), {}).update(m)

    def request(self, cols):
        self.next_id += 1
        self.p.stdin.write(json.dumps({"id": self.next_id, "cols": cols}) + "\n")
        self.p.stdin.flush()
        return self.next_id

    def get(self, rid):
        with self.lock:
            return dict(self.res.get(rid, {}))

    def close(self):
        try:
            self.p.kill()
        except Exception:
            pass


# ---------------------------------------------------------------- browser session
def page_title(page):
    try:
        return page.title()
    except Exception:
        return ""


def is_challenge(page):
    t = page_title(page).lower()
    return any(k in t for k in ("just a moment", "attention required", "cloudflare"))


def wait_challenge_clear(page):
    end = time.monotonic() + CHALLENGE_WAIT
    while time.monotonic() < end and is_challenge(page):
        page.wait_for_timeout(1000)


def wait_logged_in(page):
    end, ok_since = time.monotonic() + LOGIN_WAIT, None
    while time.monotonic() < end:
        try:
            ready = page.locator(BALANCE).first.is_visible()
            signed_out = page.locator(SIGN_IN).count() > 0
        except Exception:
            ready, signed_out = False, True
        if ready and not signed_out:
            ok_since = ok_since or time.monotonic()
            if time.monotonic() - ok_since >= 2:
                return True
        else:
            ok_since = None
        page.wait_for_timeout(400)
    return False


STEALTH_JS = """
try {
  Object.defineProperty(document, 'hidden', {get: () => false, configurable: true});
  Object.defineProperty(document, 'visibilityState', {get: () => 'visible', configurable: true});
  document.hasFocus = () => true;
  document.addEventListener('visibilitychange', e => e.stopImmediatePropagation(), true);
} catch (e) {}
"""


class Session:
    def __init__(self, p):
        self.browser = p.chromium.launch(channel="chrome", headless=HEADLESS,
                                         ignore_default_args=["--enable-automation"],
                                         args=["--disable-blink-features=AutomationControlled"])
        try:
            self.ctx = self.browser.new_context(storage_state=str(STATE_FILE),
                                                viewport={"width": 1366, "height": 800})
            self.ctx.add_init_script(STEALTH_JS)
            self.page = self.ctx.new_page()
            self.q = deque(maxlen=500)
            self.listing = None   # game id of our still-unjoined listing ("?" = created, id not seen yet)
            self.page.on("response", self._on_response)
        except Exception:
            self.browser.close()
            raise

    def _on_response(self, resp):
        try:
            if resp.request.method == "GET" and resp.ok and "api.donutflip.net" in resp.url:
                m = GAME_RE.search(resp.url)
                if m:
                    self.q.append((m.group(1), resp))
        except Exception:
            pass

    def close(self):
        try:
            self.browser.close()
        except Exception:
            pass


def read_balance(page):
    val = parse_amount(page.locator(BALANCE).first.inner_text(timeout=5000))
    if val is None:
        raise BotError("Could not parse header balance", shot(page))
    return val


def open_lobby(sess):
    """Load the Connect 4 page, verify login, return the current balance."""
    page = sess.page
    for _ in range(2):
        page.goto(LOBBY_URL, wait_until="domcontentloaded", timeout=60000)
        if is_challenge(page):
            wait_challenge_clear(page)
            if is_challenge(page):
                raise BotError(f"Cloudflare challenge did not clear (title: '{page_title(page)}').", shot(page), blocked=True)
        if wait_logged_in(page):
            return read_balance(page)
    if page.locator(SIGN_IN).count() > 0 and not is_challenge(page):
        raise BotError("Logged out: the site shows 'Sign In'.", shot(page), fatal=True)
    raise BotError(f"Page did not finish loading (title: '{page_title(page)}').", shot(page), blocked=True)


def create_game(sess, stake):
    page = sess.page
    inp = page.locator(STAKE_INPUT)
    try:
        inp.wait_for(state="visible", timeout=8000)
    except PWTimeout:  # maybe the form sits behind a "Create" tab
        for sel in ("[role=tab]:has-text('Create')", "button:text-is('Create')"):
            t = page.locator(sel).first
            if t.count():
                real_click(page, t)
                break
        inp.wait_for(state="visible", timeout=8000)
    want = int(round(stake))
    for _ in range(3):
        inp.click()
        page.keyboard.press("Control+A")
        page.keyboard.type(str(want), delay=random.randint(40, 90))
        page.wait_for_timeout(300)
        got = parse_amount(inp.input_value())
        if got is not None and int(round(got)) == want:
            break
    else:
        raise BotError(f"Could not set stake to {want} (field shows '{inp.input_value()}')", shot(page))
    sess.q.clear()
    btn = page.locator(CREATE_BTN).first
    btn.wait_for(state="visible", timeout=8000)
    if not btn.is_enabled():
        raise BotError("'Create game' button is disabled (balance too low or an open game exists?)", shot(page))
    real_click(page, btn)
    sess.listing = "?"


# ---------------------------------------------------------------- game logic
def extract_game(data):
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("moves"), list):
        return data
    g = data.get("game")
    return g if isinstance(g, dict) and isinstance(g.get("moves"), list) else None


def is_done(g):
    return (g.get("completedAt") is not None or g.get("winner") is not None or g.get("result") is not None
            or bool(re.match(r"(?i)(complete|finish|cancel|expire|abandon|ended|forfeit|closed)", str(g.get("status") or ""))))


def my_seat(game, cfg):
    forced = str(cfg.get("seat") or "auto").strip().lower()
    if forced in ("c", "creator"):
        return "creator"
    if forced in ("o", "opponent"):
        return "opponent"
    v = game.get("viewer")
    if v in SEATS:
        return v
    pid = (os.environ.get("C4_PLAYER_ID") or str(cfg.get("player_id") or "")).strip()
    if pid:
        if game.get("creatorId") == pid:
            return "creator"
        if game.get("opponentId") == pid:
            return "opponent"
    return None


def outcome_of(game, role):
    norm = lambda v: None if v is None else str(v).lower()
    Wn, R = norm(game.get("winner")), norm(game.get("result"))
    if Wn in ("draw", "tie") or R in ("draw", "tie") or (Wn is None and R in ("full", "stalemate", "board_full")):
        return "draw"
    if Wn == role:
        return "win"
    if Wn == other(role):
        return "loss"
    my_id = game.get("creatorId") if role == "creator" else game.get("opponentId")
    their_id = game.get("opponentId") if role == "creator" else game.get("creatorId")
    if game.get("winner") is not None and my_id and game["winner"] == my_id:
        return "win"
    if game.get("winner") is not None and their_id and game["winner"] == their_id:
        return "loss"
    if R in ("win", "won", "victory"):
        return "win"
    if R in ("loss", "lose", "lost", "defeat"):
        return "loss"
    if R and "win" in R and R.startswith(role):
        return "win"
    if R and "win" in R and R.startswith(other(role)):
        return "loss"
    return None


def account(game, role, sol, cfg_stake):
    """-> (outcome, profit, unknown). Unreadable results count as a loss (fail-safe)."""
    outcome = None
    if not game.get("opponentId") and isinstance(game.get("moves"), list) and not game["moves"]:
        outcome = "void"
    outcome = outcome or outcome_of(game, role)
    if not outcome:
        moves = sorted(game.get("moves") or [], key=lambda m: m.get("seq", 0))
        if sol.get("over") and moves:
            outcome = "draw" if sol.get("draw") else ("win" if moves[-1]["seat"] == role else "loss")
    unknown = False
    if not outcome:
        outcome, unknown = "loss", True
    stake = parse_amount(game.get("stake")) or cfg_stake
    profit = 0
    if outcome == "win":
        payout = parse_amount(game.get("payout")) or 0
        profit = payout - stake if payout > stake else round(stake * 0.9)
    elif outcome == "loss":
        profit = -stake
    return outcome, profit, unknown


def best_col(scores):
    b = -1
    for c in ORDER:
        if scores[c] is not None and (b < 0 or scores[c] > scores[b]):
            b = c
    return b


def dom_tone(page):
    try:
        return page.evaluate("() => { const e = document.querySelector('[class*=\"_turn\"][data-tone]'); return e ? e.dataset.tone : null; }")
    except Exception:
        return None


# ---- cancelling an unjoined listing ------------------------------------------------------------
# Two places show a Cancel button for our waiting game (never click "Call bot", the primary button next to it):
#   1) the game window:    div.pxHubq_modalActions > button.pxHubq_secondary "Cancel"  (only while the
#                          status line div.pxHubq_turn has data-tone="wait")
#   2) the listing card in the lobby: div.pxHubq_cardActions > button.pxHubq_secondary "Cancel"
WAITING_TONE = "div.pxHubq_turn[data-tone='wait']"
MODAL_CANCEL = "div.pxHubq_modalActions button.pxHubq_secondary:text-is('Cancel')"
CONFIRM_RE = re.compile(r"^\s*(confirm|yes|yes, cancel|cancel (game|battle|match|listing))\s*$", re.I)


def _card_cancel(page, gid):
    if gid and re.fullmatch(r"[0-9A-Fa-f-]{8,64}", gid):   # our card = the one whose avatar URL has our game id
        return page.locator("xpath=//div[contains(@class,'cardActions')]/button[normalize-space()='Cancel']"
                            f"[../..//img[contains(@src,'/game/{gid}/')]]")
    return page.locator("xpath=//div[contains(@class,'cardActions')]/button[normalize-space()='Cancel']")


def _press(page, loc):
    try:
        if loc.count() == 0 or not loc.first.is_visible():
            return False
        try:
            real_click(page, loc.first)
        except Exception:
            loc.first.click(timeout=3000)
        page.wait_for_timeout(800)
        extra = page.locator("[role=dialog] button, [class*='confirm'] button").filter(has_text=CONFIRM_RE)
        if extra.count():                       # in case the site asks "are you sure?"
            extra.first.click(timeout=3000)
            page.wait_for_timeout(800)
        return True
    except Exception as e:
        print("cancel click failed:", type(e).__name__, flush=True)
        return False


def cancel_listing(sess):
    """Cancel our unjoined listing (game window first, else the lobby card). True = cancelled or nothing to cancel."""
    gid = sess.listing
    if not gid:
        return True
    page = sess.page
    try:
        for attempt in range(2):
            done = False
            if page.locator(WAITING_TONE).count() and _press(page, page.locator(MODAL_CANCEL)):
                done = True
            elif _press(page, _card_cancel(page, gid)):
                done = True
            if done:
                page.wait_for_timeout(1200)
                still = (page.locator(WAITING_TONE).count() and page.locator(MODAL_CANCEL).count()) \
                    or _card_cancel(page, gid).count()
                if not still:
                    sess.listing = None
                    return True
            if attempt == 0:   # nothing visible/clickable here: open the lobby and look for the card
                page.goto(LOBBY_URL, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(2500)
    except Exception as e:
        print("cancel_listing failed:", type(e).__name__, flush=True)
    return False


def close_session(sess):
    """Cancel any waiting listing first, then close the browser."""
    if sess is None:
        return
    if sess.listing:
        ok = cancel_listing(sess)
        notify("🧹 Cancelled the unjoined listing." if ok else
               "⚠️ Could not cancel the unjoined listing. Please cancel it on the site by hand.")
    sess.close()


def waiting_banner(page):
    try:
        return bool(page.evaluate(r"""() => {
            const e = document.querySelector('[class*="_turn"][data-tone]');
            return !!e && /WAITINGFORANOPPONENT/.test(e.innerText.replace(/[\s\u00a0]+/g, '').toUpperCase());
        }"""))
    except Exception:
        return False


def diag(page, game, seat, turn, col, why):
    """One-off diagnostic. Goes to the private Discord webhook only (never the public Actions log)."""
    try:
        info = page.evaluate("""(col) => {
            const all = [...document.querySelectorAll('button[aria-label]')];
            const labels = all.map(b => b.getAttribute('aria-label')).filter(l => /column|drop|disc/i.test(l));
            const t = document.querySelector('button[aria-label="Drop a disc in column ' + (col + 1) + '"]');
            let hit = null, box = null;
            if (t) {
                const r = t.getBoundingClientRect();
                box = [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)];
                const e = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
                hit = e ? ((e === t || t.contains(e)) ? 'target' : e.tagName + '.' + String(e.className).slice(0, 60)) : null;
            }
            const tone = document.querySelector('[class*="_turn"][data-tone]');
            return {labels: labels.slice(0, 10), nLabels: labels.length, found: !!t,
                    disabled: t ? (t.disabled || t.getAttribute('aria-disabled')) : null, box, hit,
                    tone: tone ? tone.dataset.tone : null, toneText: tone ? tone.innerText.slice(0, 40) : null};
        }""", col)
    except Exception as e:
        info = {"evaluate_error": type(e).__name__}
    g = game or {}
    mv = g.get("moves") or [{}]
    notify(f"\U0001F50D Bot diagnostic: {why}\n"
           f"seat={seat} turn_calc={turn} api_turn={g.get('turn')!r} viewer={g.get('viewer')!r} "
           f"moves={len(g.get('moves') or [])} status={g.get('status')!r}\n"
           f"api_keys={sorted(g.keys())}\nmove_keys={sorted(mv[0].keys())}\npage={info}", shot(page))


def play_game(sess, solver, wait_s):
    """Wait for the listing to be joined, play it to the end. Returns (game, role, sol) or None if nobody joined."""
    page, q = sess.page, sess.q
    t_start = time.monotonic()
    gid = key = None
    game = None
    seat = None
    recv_at = seen_at = played_at = 0.0
    played_key = None
    req = None
    over_since = None
    joined_at = None
    cfg, cfg_at = get_cfg(), time.monotonic()
    diag_sent, tries = set(), {}

    while True:
        if time.monotonic() - cfg_at > 20:
            cfg, cfg_at = get_cfg(), time.monotonic()
        while q:
            rgid, resp = q.popleft()
            try:
                g = extract_game(resp.json())
            except Exception:
                continue
            if not g:
                continue
            if gid is None:
                if is_done(g) or my_seat(g, cfg) is None:
                    continue
                gid = rgid
                sess.listing = gid
                print("[game] tracking" if IN_ACTIONS else f"[game] tracking {gid}", flush=True)
            elif rgid != gid:
                continue
            game, recv_at = g, time.time()
            seat = my_seat(g, cfg) or seat
            moves = sorted(g["moves"], key=lambda m: m.get("seq", 0))
            cols = [m["column"] for m in moves]
            k = gid + ":" + "".join(map(str, cols))
            if k != key:
                key, seen_at, over_since = k, time.time(), None
                req = solver.request(cols) if cols else None
            if g.get("opponentId") or moves:
                joined_at = joined_at or time.monotonic()
                sess.listing = None          # someone joined: nothing to cancel any more

        if game is None:
            if time.monotonic() - t_start > 45 and gid is None:
                raise BotError("Created a game but never saw it in the API. If 'viewer' is missing from the "
                               "response, set the C4_PLAYER_ID secret.", shot(page))
            page.wait_for_timeout(200)
            continue

        moves = sorted(game["moves"], key=lambda m: m.get("seq", 0))
        n = len(moves)
        sol = solver.get(req) if req else {}

        if is_done(game):
            sess.listing = None
            return game, seat, sol
        if sol.get("over"):
            over_since = over_since or time.monotonic()
            if time.monotonic() - over_since > 15:   # API never showed a final state: settle from the board
                return game, seat, sol
            page.wait_for_timeout(250)
            continue
        if joined_at is None:
            if wait_s and time.monotonic() - t_start > wait_s:   # opponent_wait_s = 0 -> wait forever
                return None
            if not cfg["enabled"] or (MAX_RUNTIME_MIN and (time.monotonic() - RUN_START) / 60 > MAX_RUNTIME_MIN):
                return None   # nobody joined yet: let the run stop instead of waiting

        # ---- whose turn ----
        turn = game.get("turn") if game.get("turn") in SEATS else None
        if turn is None and n > 0:
            turn = moves[0]["seat"] if n % 2 == 0 else other(moves[0]["seat"])
        if turn is None and (game.get("opponentId") or n):
            turn = dom_tone(page)
        if joined_at and time.time() - seen_at > 60 and "stall" not in diag_sent:
            diag_sent.add("stall")
            diag(page, game, seat, turn, 3, "no position change for 60s (possible stall; check turn_calc vs seat)")
        if turn != seat or (time.time() - seen_at) * 1000 < cfg["min_delay_ms"]:
            page.wait_for_timeout(150)
            continue
        if played_key == key and time.time() - played_at < 2.5:
            page.wait_for_timeout(150)
            continue

        heights = [0] * W
        for m in moves:
            if 0 <= m["column"] < W:
                heights[m["column"]] += 1
        legal = [c for c in ORDER if heights[c] < H]
        if not legal:
            page.wait_for_timeout(200)
            continue

        te, sn = game.get("turnEndsAt"), game.get("serverNow")
        try:
            rem = (float(te) - float(sn)) / 1000 - (time.time() - recv_at) if te and sn else None
        except (TypeError, ValueError):
            rem = None
        late = rem <= cfg["safe_remaining_s"] if rem is not None else (time.time() - seen_at) > 4

        col, ready = -1, False
        if n == 0:
            col, ready = 3, True
        elif len(legal) == 1:
            col, ready = legal[0], True
        elif sol.get("scores"):
            b = sol.get("best")
            col = b if isinstance(b, int) and b >= 0 else best_col(sol["scores"])
            ready = bool(sol.get("exact") or sol.get("final") or sol.get("bestProven"))
        if not ready and not late:
            page.wait_for_timeout(100)
            continue
        if col < 0 or col not in legal:
            if not late:
                page.wait_for_timeout(100)
                continue
            col = legal[0]

        btn = page.locator(f'button[aria-label="Drop a disc in column {col + 1}"]').first
        try:
            if btn.count() and btn.is_enabled():
                real_click(page, btn)
                tries.pop("_nb", None)
                played_key, played_at = key, time.time()
                tries[key] = tries.get(key, 0) + 1
                if tries[key] == 3 and "noeffect" not in diag_sent:
                    diag_sent.add("noeffect")
                    diag(page, game, seat, turn, col, "clicked this column 3 times but the position never changed")
            elif waiting_banner(page):
                tries.pop("_nb", None)        # nobody has joined yet: disabled buttons are expected
                page.wait_for_timeout(500)
            else:
                tries.setdefault("_nb", time.monotonic())
                if time.monotonic() - tries["_nb"] > 8 and "nobtn" not in diag_sent:
                    diag_sent.add("nobtn")
                    diag(page, game, seat, turn, col, "opponent present but column button still missing/disabled after 8s")
        except Exception as e:
            print("click failed:", type(e).__name__, flush=True)
            if "exc" not in diag_sent:
                diag_sent.add("exc")
                diag(page, game, seat, turn, col, f"click raised {type(e).__name__}: {str(e)[:150]}")
        page.wait_for_timeout(150)


# ---------------------------------------------------------------- main loop
class Stop(Exception):
    pass


def main():
    signal.signal(signal.SIGTERM, lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    t_run = time.monotonic()
    stats = load_stats()
    stats.setdefault("pnl", 0)
    stats.setdefault("rec", {"w": 0, "l": 0, "d": 0, "v": 0, "u": 0})
    src_hash = prepare_state()
    if stats.get("dead_hash") == src_hash:
        print("Login state is known to be logged out and has not been replaced. Exiting quietly.")
        return 0
    stats.pop("dead_hash", None)

    cfg = get_cfg()
    if not cfg["enabled"]:
        print("Disabled in config.json. Exiting.")
        return 0
    if cfg.get("reset_pnl_token") == stats.get("reset_token") and cfg["max_loss"] \
            and stats["pnl"] <= -parse_amount(cfg["max_loss"]):
        print("Max loss already reached (change reset_pnl_token or max_loss to continue). Exiting.")
        return 0
    solver = Solver()
    notify(f"▶️ Connect 4 bot started · {now_str()}\nBooks: {solver.books}\n"
           f"Stake {fmt(parse_amount(cfg['stake']))} · seat {cfg['seat']} · P/L so far {fmt_signed(stats['pnl'])}")
    games = errors = 0
    blocked_n, blocked_since = 0, None

    def nap(seconds):
        """Sleep, but wake up to honour 'enabled: false' and the runtime limit. False = stop now."""
        end, last_check = time.monotonic() + seconds, time.monotonic()
        while time.monotonic() < end:
            time.sleep(min(15, max(end - time.monotonic(), 0)))
            if MAX_RUNTIME_MIN and (time.monotonic() - t_run) / 60 > MAX_RUNTIME_MIN:
                return False
            if time.monotonic() - last_check > 120:
                last_check = time.monotonic()
                if not get_cfg()["enabled"]:
                    return False
        return True

    exit_code, reason = 0, "stopped"
    sess = None
    lobby_ready = False
    try:
        with sync_playwright() as p:
            while True:
                cfg = get_cfg()
                if cfg.get("reset_pnl_token") != stats.get("reset_token"):
                    stats["pnl"], stats["rec"], stats["reset_token"] = 0, {"w": 0, "l": 0, "d": 0, "v": 0, "u": 0}, cfg.get("reset_pnl_token")
                stake = parse_amount(cfg["stake"])
                if not cfg["enabled"]:
                    reason = "disabled in config.json"; break
                if cfg["max_loss"] and stats["pnl"] <= -parse_amount(cfg["max_loss"]):
                    reason = f"max loss reached (P/L {fmt_signed(stats['pnl'])})"; break
                if cfg["max_games"] and games >= int(cfg["max_games"]):
                    reason = f"max_games ({cfg['max_games']}) reached"; break
                if MAX_RUNTIME_MIN and (time.monotonic() - t_run) / 60 > MAX_RUNTIME_MIN:
                    reason = "runtime limit reached (next scheduled run continues)"; break

                try:
                    if sess is None:
                        sess = Session(p)
                        lobby_ready = False
                    if not lobby_ready:
                        bal = open_lobby(sess)
                        if blocked_n:
                            notify(f"✅ Cloudflare cleared after {blocked_n} retries "
                                   f"({(time.monotonic() - blocked_since) / 60:.0f} min). Resuming.")
                            blocked_n, blocked_since = 0, None
                    lobby_ready = False
                    if bal < stake or (cfg["min_balance"] and bal < parse_amount(cfg["min_balance"])):
                        reason = f"balance {fmt(bal)} is below the stake/min_balance"; break

                    create_game(sess, stake)
                    result = play_game(sess, solver, float(cfg["opponent_wait_s"]))
                    if result is None:
                        ok = cancel_listing(sess)      # never leave an unjoined listing behind
                        notify("⌛ Stopped waiting for an opponent · " +
                               ("listing cancelled." if ok else "COULD NOT CANCEL the listing, check the site."))
                        lobby_ready = False
                        continue
                    game, role, sol = result
                    outcome, profit, unknown = account(game, role or "creator", sol, stake)
                    rec = stats["rec"]
                    rec["u" if unknown else {"win": "w", "loss": "l", "draw": "d", "void": "v"}[outcome]] += 1
                    stats["pnl"] += profit
                    games += 1
                    errors = 0

                    save_stats(stats)
                    sess.page.wait_for_timeout(int(PAYOUT_SETTLE_S * 1000))
                    try:
                        bal = open_lobby(sess)   # fresh load = true balance after payout
                        lobby_ready = True
                        save_state(sess.ctx)
                    except BotError as be:
                        if be.fatal or not be.blocked:
                            raise
                        bal, lobby_ready = None, False   # blocked: the next loop retries with backoff
                    ne = int(cfg["notify_every"] or 0)
                    if ne and games % ne == 0:
                        notify(f"🎮 **{outcome.upper()}{'?' if unknown else ''}** {fmt_signed(profit)} · stake {fmt(stake)} · {now_str()}\n"
                               f"Balance: {fmt(bal) if bal is not None else 'unknown (page blocked)'}\n"
                               f"P/L: {fmt_signed(stats['pnl'])} · {rec['w']}W {rec['l']}L {rec['d']}D"
                               + (f" {rec['u']}?" if rec['u'] else "") + f" · games this run: {games}")
                    time.sleep(END_BUFFER_S)

                except BotError as e:
                    if e.blocked:
                        blocked_n += 1
                        blocked_since = blocked_since or time.monotonic()
                        steps = cfg["cf_retry_minutes"] or [5]
                        wait_min = steps[min(blocked_n - 1, len(steps) - 1)] * random.uniform(0.9, 1.2)
                        blocked_for = (time.monotonic() - blocked_since) / 60
                        if blocked_for + wait_min > float(cfg["cf_give_up_min"]):
                            notify(f"🛑 Blocked by Cloudflare for {blocked_for:.0f} min. Giving up for this run; "
                                   f"the next scheduled run will try again.", e.shot)
                            exit_code, reason = 1, "blocked by Cloudflare"
                            break
                        if blocked_n == 1 or blocked_n % 3 == 0:
                            notify(f"🛡️ Blocked ({e}) · attempt {blocked_n}. Retrying in {wait_min:.0f} min.",
                                   e.shot if blocked_n == 1 else None)
                        close_session(sess); sess = None   # fresh browser for the next try
                        lobby_ready = False
                        if not nap(wait_min * 60):
                            reason = "stopped while waiting out a block"
                            break
                        continue
                    notify(f"❌ **Error** · {now_str()}\n{e}", e.shot)
                    if e.fatal:
                        stats["dead_hash"] = src_hash
                        notify("🔑 **Login state is dead. Reload it:** run `python login.py --encrypt` on your PC "
                               "(Chrome open with --remote-debugging-port=9222 and logged in), then upload the new "
                               "`state.enc` to the private data repo. The bot stays quiet until the state changes.")
                        exit_code, reason = 2, "logged out"; break
                    errors += 1
                except Exception as e:
                    notify(f"❌ **Unexpected error** · {now_str()}\n{type(e).__name__}: {e}",
                           shot(sess.page) if sess else None)
                    errors += 1

                if errors:
                    close_session(sess); sess = None
                    if errors >= MAX_CONSECUTIVE_ERRORS:
                        exit_code, reason = 1, f"{errors} consecutive errors"; break
                    time.sleep(ERROR_BACKOFF)
    except KeyboardInterrupt:
        reason = "interrupted (workflow cancelled?)"
    finally:
        if sess:
            try:
                if sess.listing:           # terminated while waiting for an opponent: cancel the listing first
                    ok = cancel_listing(sess)
                    notify("🧹 Cancelled the unjoined listing before stopping." if ok else
                           "⚠️ Could not cancel the unjoined listing. Please cancel it on the site by hand.")
            except BaseException:
                pass
            if exit_code != 2 and not errors:
                save_state(sess.ctx)
            sess.close()
        save_stats(stats)
        solver.close()
        notify(f"⏹️ Connect 4 bot {reason} · games this run: {games} · P/L {fmt_signed(stats['pnl'])}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
