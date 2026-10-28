"""DonutFlip Connect 4 bot (create mode only), designed to run on GitHub Actions from a PUBLIC repo.

Nothing private lives in the public repo: config.json, state.enc and stats.enc are kept in a
separate private data repo (see store.py). config.json is re-read before every game.
Notifications: Discord webhook (DISCORD_WEBHOOK_URL secret or .secret file).
Solver: solver.js driven through solver_server.js (Node).
"""
import base64, hashlib, json, os, random, re, signal, subprocess, sys, threading, time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from store import Store
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
CODE_REV = "2026-10-28-slots+claims+nowindow"   # shown in the debug log (ENV line) so you can tell which code ran
HEADLESS = "--headed" not in sys.argv
LOBBY_URL = "https://donutflip.net/connect-four"
HOME_URL = "https://donutflip.net/"

STORE = Store()   # reads C4_DATA_REPO / C4_DATA_TOKEN / C4_DATA_PREFIX (e.g. accounts/alt)
ACCOUNT = os.environ.get("C4_ACCOUNT", "").strip()
SLOT = os.environ.get("C4_SLOT", "").strip().lstrip("-")   # "" = the account's main session; "1", "2" = extra sessions
SFX = f"-{SLOT}" if SLOT else ""
STATE_NAME, STATS_NAME = f"state{SFX}.enc", f"stats{SFX}.enc"   # per-session files inside accounts/<account>/
LABEL = f"[{ACCOUNT}{SFX}] " if ACCOUNT else (f"[{SLOT}] " if SLOT else "")
ME = (ACCOUNT or "account") + SFX        # this session's name in claims.json
IN_ACTIONS = bool(os.environ.get("GITHUB_ACTIONS"))
CONFIG_FILE = HERE / "config.json"         # local-dev fallback (gitignored)
STATE_ENC = HERE / STATE_NAME             # local-dev fallback (gitignored)
STATE_FILE = HERE / "storage_state.json"   # decrypted copy, never committed
STATS_FILE = HERE / STATS_NAME            # local-dev fallback (gitignored)
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
    "retry_ms": 1500,           # re-click if the position has not changed this long after a click (fast and human attempts alternate)
    "human_steps": 3,           # mouse-move steps of the human click (each step costs ~0.25 s on a busy runner)
    "click_mode": "fast",       # "fast" = one in-page click (like the old extension), "human" = scroll + mouse path (slow: ~0.25 s per mouse step on the runner)
    "webhook": "",              # this account's Discord webhook (private repo only); falls back to the DISCORD_WEBHOOK_URL env / .secret
    "debug": 0,                 # 0 = off, 1 = timing log sent to the private Discord webhook after each game, 2 = also print to stdout
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


class Yielded(Exception):
    """This session was following a game that another session of the same account already follows: it gave up its window."""
    def __init__(self, gid):
        super().__init__(f"yielded game {gid}")
        self.gid = gid


class NoWindow(Exception):
    """We clicked Create but no game window / unique game API showed up in this browser."""


class BotError(Exception):
    def __init__(self, msg, shot=None, fatal=False, blocked=False, limit=False):
        super().__init__(msg)
        self.shot, self.fatal, self.blocked, self.limit = shot, fatal, blocked, limit


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


# ---------------------------------------------------------------- debug log (opt-in: "debug" in the private config.json)
# "debug": 1 -> buffer everything and upload it to the PRIVATE Discord webhook as c4-debug.txt after each game
# "debug": 2 -> additionally print the non-private lines to stdout (a PUBLIC repo's Actions log is public!)
# Takes effect live (config.json is re-read between games and every 20 s during a game). Env C4_DEBUG / --debug also force it on.
# Never logged: game id, stake, balance, player id. "private" lines (winner/result/move list) never go to stdout.
_ENV_DEBUG = os.environ.get("C4_DEBUG", "").strip()
DEBUG = bool(_ENV_DEBUG) or "--debug" in sys.argv
DEBUG_STDOUT = _ENV_DEBUG == "2"


def set_debug(cfg):
    global DEBUG, DEBUG_STDOUT
    try:
        lvl = int(cfg.get("debug") or 0)
    except (TypeError, ValueError):
        lvl = 0
    if _ENV_DEBUG or "--debug" in sys.argv:
        lvl = max(lvl, int(_ENV_DEBUG) if _ENV_DEBUG.isdigit() else 1)
    DEBUG, DEBUG_STDOUT = lvl > 0, lvl >= 2
_DBG_T0 = time.monotonic()
_DBG_BUF = []


def _f(x, nd=2):
    return "None" if x is None else f"{x:.{nd}f}"


def dlog(tag, _priv=False, **kv):
    if not DEBUG:
        return
    line = f"[{time.monotonic() - _DBG_T0:9.3f}] {tag:<9} " + " ".join(f"{k}={v}" for k, v in kv.items())
    _DBG_BUF.append((line, _priv))
    if len(_DBG_BUF) > 30000:
        del _DBG_BUF[:5000]
    if DEBUG_STDOUT and not _priv:
        print("[dbg] " + line, flush=True)


def dflush(label):
    if not DEBUG or not _DBG_BUF:
        return
    lines = list(_DBG_BUF)
    _DBG_BUF.clear()
    text = "\n".join(ln for ln, _ in lines) + "\n"
    try:
        with open(HERE / "c4-debug.log", "a", encoding="utf-8") as fh:
            fh.write(text)
    except Exception:
        pass
    ok = False
    if WEBHOOK:
        try:
            payload = {"content": f"\U0001F41E debug log ({label}) \u00b7 {now_str()}", "allowed_mentions": {"parse": []}}
            r = requests.post(WEBHOOK, data={"payload_json": json.dumps(payload)},
                              files={"file": ("c4-debug.txt", text.encode("utf-8"), "text/plain")}, timeout=30)
            ok = r.ok
            if not ok:
                print("debug upload failed:", r.status_code, flush=True)
        except Exception as e:
            print("debug upload failed:", type(e).__name__, flush=True)
    if not ok and not DEBUG_STDOUT:   # no webhook / upload failed: fall back to stdout (non-private lines only)
        print("[dbg] ---- debug log (" + label + ") ----", flush=True)
        print("\n".join(ln for ln, priv in lines if not priv), flush=True)


def sc_str(scores):
    return "[" + ",".join("-" if v is None else str(v) for v in scores) + "]"


def sol_sum(sol):
    if not sol:
        return "none"
    keys = ("depth", "stage", "final", "exact", "bestProven", "best", "outcome", "exactTimeout", "weak", "book", "_t")
    out = ",".join(f"{k}={sol[k]}" for k in keys if k in sol)
    if sol.get("scores"):
        out += " scores=" + sc_str(sol["scores"])
    return out


def dom_probe(page):
    """(on-screen turn timer text, round-trip ms of one trivial page.evaluate = browser-control latency)"""
    t = time.monotonic()
    try:
        txt = page.evaluate("() => { const t = document.querySelector('[class*=\"_turnTime\"]'); return t ? t.textContent : null; }")
    except Exception as e:
        txt = "err:" + type(e).__name__
    return txt, (time.monotonic() - t) * 1000


def dbg_end(game, seat, sol, why):
    if not DEBUG:
        return
    mv = sorted(game.get("moves") or [], key=lambda m: m.get("seq", 0))
    dlog("GAMEEND", why=why, moves=len(mv),
         my_auto_moves=sum(1 for m in mv if m.get("auto") and m.get("seat") == seat),
         opp_auto_moves=sum(1 for m in mv if m.get("auto") and m.get("seat") != seat),
         timeouts=game.get("timeouts"), solver_over=sol.get("over") if sol else None)
    dlog("GAMEEND+", _priv=True, status=game.get("status"), winner=game.get("winner"), result=game.get("result"),
         seat=seat, seq="".join(str(m["column"] + 1) for m in mv))


def notify_bg(msg, screenshot=None):
    threading.Thread(target=notify, args=(msg, screenshot), daemon=True).start()


def shot(page):
    try:
        return page.screenshot()
    except Exception:
        return None


def real_click(page, locator, tm=None, quick=False, steps=3):
    """Human-like click. quick=True (used for moves): no actionability waits, one plain evaluate for the box,
    only scroll when the button is off-screen, few mouse steps, no pause."""
    t0 = time.monotonic()
    if quick:
        r = locator.evaluate("e => { const b = e.getBoundingClientRect(); return [b.x, b.y, b.width, b.height, innerWidth, innerHeight]; }",
                             timeout=5000)
        box = {"x": r[0], "y": r[1], "width": r[2], "height": r[3]}
        if not (box["width"] > 0 and box["height"] > 0 and box["x"] >= 0 and box["y"] >= 0
                and box["x"] + box["width"] <= r[4] and box["y"] + box["height"] <= r[5]):
            locator.scroll_into_view_if_needed(timeout=3000)
            box = locator.bounding_box()
        t1 = t2 = time.monotonic()
        nsteps, pause = max(1, int(steps)), 0
    else:
        locator.scroll_into_view_if_needed(timeout=10000)
        t1 = time.monotonic()
        box = locator.bounding_box()
        t2 = time.monotonic()
        nsteps, pause = random.randint(8, 20), random.randint(80, 250)
    if not box:
        raise BotError("Element has no bounding box", shot(page))
    x = box["x"] + box["width"] * random.uniform(0.35, 0.65)
    y = box["y"] + box["height"] * random.uniform(0.35, 0.65)
    page.mouse.move(x, y, steps=nsteps)
    t3 = time.monotonic()
    if pause:
        page.wait_for_timeout(pause)
    t4 = time.monotonic()
    page.mouse.click(x, y)
    if tm is not None:
        tm.update(scroll_ms=round((t1 - t0) * 1000), bbox_ms=round((t2 - t1) * 1000), move_ms=round((t3 - t2) * 1000),
                  move_steps=nsteps, pause_ms=round((t4 - t3) * 1000), click_ms=round((time.monotonic() - t4) * 1000))


# One browser round-trip does everything: checks that the page agrees it is OUR turn and the board is unlocked, then
# (fast mode) clicks. Locator-based calls cost several round-trips and a 1 s timeout was routinely exceeded on a busy
# runner (browser-control latency ~250 ms), which used to be mis-reported as "button missing".
CLICK_JS = """([col, seat, doClick]) => {
  const t = document.querySelector('[class*="_turn"][data-tone]');
  const tone = t ? t.dataset.tone : null;
  if (tone && tone !== 'creator' && tone !== 'opponent' && tone !== 'wait') return 'over';
  if (seat && (tone === 'creator' || tone === 'opponent') && tone !== seat) return 'notmine';
  const b = document.querySelector('button[aria-label="Drop a disc in column ' + (col + 1) + '"]');
  if (!b) return 'missing';
  if (b.disabled || b.getAttribute('aria-disabled') === 'true') return 'disabled';
  if (doClick) b.click();
  return 'ok';
}"""


def attempt_click(page, col, cfg, attempt, seat, tm=None):
    """One click attempt -> 'ok' | 'notmine' | 'over' | 'disabled' | 'missing' | 'slow' | 'err:<Name>'. Never blocks long.
    Attempts alternate: 0 = fast in-page click, 1 = quick human click, 2 = fast, ... (click_mode 'human' = always human)."""
    fast = str(cfg.get("click_mode", "fast")).lower() != "human" and attempt % 2 == 0
    t_ev = time.monotonic()
    try:
        res = page.evaluate(CLICK_JS, [col, seat, fast])
    except Exception as e:
        res = "slow" if "Timeout" in type(e).__name__ else "err:" + type(e).__name__
    if tm is not None:
        tm.update(mode="fast" if fast else "human", res=res, eval_ms=round((time.monotonic() - t_ev) * 1000))
    if res != "ok" or fast:
        return res
    btn = page.locator(f'button[aria-label="Drop a disc in column {col + 1}"]').first
    real_click(page, btn, tm, quick=True, steps=cfg.get("human_steps", 3))
    return "ok"


# ---------------------------------------------------------------- live config (the control surface)
_last_cfg = dict(DEFAULT_CFG)
_cfg_warned = False


def get_cfg():
    """config.json from the private data repo (edits apply live), else the local file."""
    global _last_cfg, _cfg_warned, WEBHOOK
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
            data = json.loads(raw)
            if SLOT:   # extra session: base settings + the overrides in "slots": {"1": {...}}; not listed = off
                ov = (data.get("slots") or {}).get(SLOT)
                if isinstance(ov, dict):
                    data.update({k: v for k, v in ov.items() if not k.startswith("_")})
                else:
                    data["enabled"] = False
            data.pop("slots", None)
            cfg = dict(DEFAULT_CFG)
            cfg.update({k: v for k, v in data.items() if not k.startswith("_")})
            if parse_amount(cfg["stake"]) in (None, 0):
                raise ValueError(f"bad stake {cfg['stake']!r}")
            _last_cfg, _cfg_warned = cfg, False
            hook = str(cfg.get("webhook") or "").strip().strip("\"'")
            if hook.startswith("https://"):
                WEBHOOK = hook
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
        blob = STORE.get(STATS_NAME) if STORE.enabled else (STATS_FILE.read_bytes() if STATS_FILE.exists() else None)
        return json.loads(_unseal(blob)) if blob else {}
    except Exception as e:
        print("could not load stats:", type(e).__name__, flush=True)
        return {}


def save_stats(st):
    blob = _seal(json.dumps(st).encode())
    try:
        if STORE.enabled:
            STORE.put(STATS_NAME, blob, "stats")
        else:
            STATS_FILE.write_bytes(blob)
    except Exception as e:
        print("could not save stats:", type(e).__name__, flush=True)


def record_game(stats, outcome, profit, unknown):
    """Hourly UTC buckets (tz-independent) so summary.py can build daily reports in any timezone."""
    key = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    b = stats.setdefault("hourly", {}).setdefault(key, {"pnl": 0, "g": 0, "w": 0, "l": 0, "d": 0, "v": 0, "u": 0})
    b["pnl"] += profit
    b["g"] += 1
    b["u" if unknown else {"win": "w", "loss": "l", "draw": "d", "void": "v"}[outcome]] += 1
    cutoff = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y-%m-%dT%H")
    for k in [k for k in stats["hourly"] if k < cutoff]:
        del stats["hourly"][k]


def prepare_state():
    """Create storage_state.json from the private repo's state.enc (preferred), a local state.enc,
    or the STATE_JSON secret. Returns a hash of the source."""
    f = _fernet()
    blob = None
    if STORE.enabled:
        blob = STORE.get(STATE_NAME)
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
            STORE.put(STATE_NAME, blob, "session")
        else:
            STATE_ENC.write_bytes(blob)
    except Exception as e:
        print("could not save state:", type(e).__name__, flush=True)


# ---------------------------------------------------------------- solver (Node subprocess)
class Solver:
    def __init__(self):
        self.res, self.next_id, self.books = {}, 0, {}
        self.req_t = {}
        self._t_init = time.monotonic()
        self.ready = threading.Event()
        self.lock = threading.Lock()
        self.p = subprocess.Popen(["node", str(HERE / "solver_server.js"), BOOKS_DIR, "24", "7000"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                  bufsize=1, cwd=str(HERE))
        threading.Thread(target=self._read, daemon=True).start()
        if not self.ready.wait(180):
            raise RuntimeError("solver did not start")
        dlog("SOLVERUP", startup_s=_f(time.monotonic() - self._t_init), books=self.books)

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
            if DEBUG:
                rt = self.req_t.get(m.get("id"))
                dlog("SOLVER", id=m.get("id"), since_req_ms=f"{(time.monotonic() - rt) * 1000:.0f}" if rt else "?",
                     node_ms=m.get("_t"), **{k: m[k] for k in ("depth", "stage", "final", "exact", "bestProven", "best", "outcome",
                                                              "exactTimeout", "weak", "book", "over", "draw", "error", "exactSkipped")
                                             if k in m},
                     scores=sc_str(m["scores"]) if m.get("scores") else None)
            with self.lock:
                self.res.setdefault(m.get("id"), {}).update(m)

    def request(self, cols):
        self.next_id += 1
        self.req_t[self.next_id] = time.monotonic()
        dlog("REQ", id=self.next_id, n=len(cols))
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
        dlog("BROWSER", version=self.browser.version, headless=HEADLESS)
        try:
            self.ctx = self.browser.new_context(storage_state=str(STATE_FILE),
                                                viewport={"width": 1366, "height": 800})
            self.ctx.add_init_script(STEALTH_JS)
            self.page = self.ctx.new_page()
            self.q = deque(maxlen=500)
            self.api_bad = deque(maxlen=20)   # (http status, method, time) of failed api.donutflip.net responses
            self.created = False   # we clicked "Create game" and have not yet adopted our own game
            self.dup = None        # set (by the claims thread) to the name of an earlier session following the same game
            self.listing = None   # game id of our still-unjoined listing ("?" = created, id not seen yet)
            self.page.on("response", self._on_response)
        except Exception:
            self.browser.close()
            raise

    def _on_response(self, resp):
        try:
            if "api.donutflip.net" in resp.url and not resp.ok:
                self.api_bad.append((resp.status, resp.request.method, time.time()))
            if resp.request.method == "GET" and resp.ok and "api.donutflip.net" in resp.url:
                m = GAME_RE.search(resp.url)
                if m:
                    self.q.append((m.group(1), resp, time.time()))
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
    sess.created, sess.dup = False, None
    close_game_window(page)   # a game of another session may have popped up over the lobby
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
    close_game_window(page)   # it may also pop up while the stake is typed
    sess.q.clear()
    btn = page.locator(CREATE_BTN).first
    btn.wait_for(state="visible", timeout=8000)
    if not btn.is_enabled():
        raise BotError("'Create game' button is disabled (balance too low or an open game exists?)", shot(page))
    real_click(page, btn)
    sess.listing = "?"
    sess.created = True
    # At the open-game limit the site creates nothing and shows: "You can have up to 3 open games"
    end = time.monotonic() + 3.5
    while time.monotonic() < end:
        try:
            err = page.locator("div.pxHubq_error[role=alert]")
            if err.count() and err.first.is_visible():
                txt = err.first.inner_text(timeout=1500).strip()
                if re.search(r"(?i)open games|up to \d+", txt):
                    try:
                        page.locator("div.pxHubq_error button[aria-label='Dismiss error']").first.click(timeout=2000)
                    except Exception:
                        pass
                    sess.listing = None
                    raise BotError("open-game limit reached: " + txt[:80], limit=True)
                break   # some other error text: the 45 s "never saw it in the API" check reports it
        except BotError:
            raise
        except Exception:
            pass
        page.wait_for_timeout(300)


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


TURN_TONES = ("creator", "opponent")


def dom_view(page):
    """(status-line tone, any column button enabled) in ONE round trip.
    tone is the site's own data-tone: 'creator' / 'opponent' (whose turn, whatever the opponent's name is),
    'wait', or a result tone (win/loss/draw...). Opponent names in the banner text are never used."""
    try:
        r = page.evaluate("""() => {
            const t = document.querySelector('[class*="_turn"][data-tone]');
            const bs = [...document.querySelectorAll('button[aria-label^="Drop a disc in column"]')];
            return [t ? t.dataset.tone : null,
                    bs.some(b => !b.disabled && b.getAttribute('aria-disabled') !== 'true')];
        }""")
        return r[0], bool(r[1])
    except Exception:
        return None, False


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
    # game id unknown: cancel NOTHING from the lobby (other sessions of this account have Cancel buttons too)
    return page.locator("xpath=//div[@id='__no_such_card__']")


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
    notify_bg(f"\U0001F50D Bot diagnostic: {why}\n"
           f"seat={seat} turn_calc={turn} api_turn={g.get('turn')!r} viewer={g.get('viewer')!r} "
           f"moves={len(g.get('moves') or [])} status={g.get('status')!r}\n"
           f"api_keys={sorted(g.keys())}\nmove_keys={sorted(mv[0].keys())}\npage={info}", shot(page))


# ---------------------------------------------------------------- one browser per game (sessions of one account)
# Every session of an account is a separate browser. A game window that pops up in a browser sitting in the lobby can
# show a game another session already plays (both then receive the same API). The sessions share claims.json in the
# private data repo ({session: {gid, seen, ts}}); the session that saw a game LATER closes its window (Esc) and goes on to create its own game. Without STORE (local use) none of this is active.
CSTORE = Store(prefix=f"accounts/{ACCOUNT}") if (ACCOUNT and STORE.enabled) else None
CLAIMS_FILE = "claims.json"
_claims_lock = threading.Lock()
_claim_state = {"on": False}
WINDOW = "section.pxHubq_modal[aria-label='Connect Four game']"
CARD_GID_RE = re.compile(r"/connect-four/game/([0-9A-Fa-f-]{8,64})/")


def claims_read():
    if CSTORE is None:
        return {}
    blob = CSTORE.get(CLAIMS_FILE)
    data = json.loads(blob.decode("utf-8-sig")) if blob else {}
    cutoff = time.time() - 3 * 3600
    return {k: v for k, v in data.items() if isinstance(v, dict) and (v.get("ts") or 0) > cutoff}


def claims_set(entry):
    """entry = dict (follow this game) or None (release). Read-merge-write with retries: sessions never overwrite each other."""
    if CSTORE is None:
        return False
    for attempt in range(8):
        try:
            with _claims_lock:
                data = claims_read()          # also refreshes the file's sha
                if entry is None:
                    data.pop(ME, None)
                else:
                    data[ME] = entry
                CSTORE.put_once(CLAIMS_FILE, (json.dumps(data) + "\n").encode(), "claims")
            return True
        except requests.HTTPError as e:
            if getattr(e.response, "status_code", 0) not in (409, 422, 500, 502, 503):
                break
        except Exception:
            break
        time.sleep(random.uniform(0.15, 0.6) * (attempt + 1))
    dlog("CLAIMFAIL", entry=int(entry is not None))
    return False


def duplicate_of(gid, my_seen):
    """Name of another session that follows `gid` and saw it earlier than we did (we must then give up), else None."""
    try:
        data = claims_read()
    except Exception as e:
        dlog("CLAIMERR", err=type(e).__name__)
        return None
    for k, v in data.items():
        if k != ME and v.get("gid") == gid:
            try:
                if (float(v.get("seen") or 0), k) < (float(my_seen), ME):
                    return k
            except (TypeError, ValueError):
                return k
    return None


def claim_async(sess, gid, seen):
    """Publish 'I follow gid', then re-check a few seconds later (covers two sessions adopting at the very same moment)."""
    _claim_state["on"] = True

    def work():
        claims_set({"gid": gid, "seen": seen, "ts": time.time()})
        time.sleep(2.5)
        k = duplicate_of(gid, seen)
        if k:
            sess.dup = k
    threading.Thread(target=work, daemon=True).start()


def claim_release_async():
    if _claim_state["on"]:
        _claim_state["on"] = False
        threading.Thread(target=claims_set, args=(None,), daemon=True).start()


def close_game_window(page):
    """Close an open game window with Esc (fast and reliable); background click and the X button are fallbacks.
    True = no window left."""
    for attempt in range(4):
        try:
            if page.locator(WINDOW).count() == 0:
                return True
            if attempt < 2:
                page.keyboard.press("Escape")
            elif attempt == 2:
                page.mouse.click(6, 6)                    # overlay corner, far from the centred window
            else:
                page.locator("button[aria-label='Close game']").first.click(timeout=3000)   # last resort
            page.wait_for_timeout(400)
        except Exception:
            pass
    try:
        return page.locator(WINDOW).count() == 0
    except Exception:
        return False


def own_waiting_gids(page):
    """Game ids of the lobby cards that have a Cancel button (= the waiting listings of this account)."""
    try:
        srcs = page.evaluate("""() => [...document.querySelectorAll('div[class*=cardActions] > button')]
            .filter(b => b.textContent.trim() === 'Cancel')
            .map(b => { const a = b.closest('article'); const i = a ? a.querySelector('img[src*="/game/"]') : null; return i ? i.src : null; })""")
    except Exception:
        return set()
    return {m.group(1) for m in (CARD_GID_RE.search(x or "") for x in srcs) if m}


NO_WINDOW_S = 8   # seconds after Create without a game window / unique API before we look at the listings


def window_open(page):
    try:
        return page.locator(WINDOW).count() > 0
    except Exception:
        return False


def reconcile_extras(sess, exclude=(), reload_first=True):
    """Cancel the waiting listings of this account that no session follows (a listing without an open window cannot be
    played, it would be lost to inactivity). A card counts as 'extra' only if it is unclaimed in TWO reads 3 s apart,
    because another session's claim can still be on its way. Returns the number of listings cancelled."""
    page = sess.page
    sess.listing = None

    def extras():
        gids = own_waiting_gids(page) - set(exclude)
        claimed = {v.get("gid") for k, v in claims_read().items() if k != ME}
        return gids - claimed, len(gids)

    try:
        if reload_first:
            open_lobby(sess)
        e1, n1 = extras()
        dlog("EXTRAS", read=1, waiting=n1, unclaimed=len(e1))
        if not e1:
            return 0                     # nothing was created: the caller simply creates again
        page.wait_for_timeout(3000)
        open_lobby(sess)
        e2, n2 = extras()
        dlog("EXTRAS", read=2, waiting=n2, unclaimed=len(e2))
        done = 0
        for gid in sorted(e1 & e2):
            sess.listing = gid
            if cancel_listing(sess):
                done += 1
            sess.listing = None
        print(f"[game] cancelled {done} unclaimed listing(s) that had no window", flush=True)
        return done
    except BotError:
        raise
    except Exception as e:
        print("reconcile failed:", type(e).__name__, flush=True)
        return 0
    finally:
        sess.listing = None


def never_saw_error(sess):
    page = sess.page
    bad = [st for st, _, ts in sess.api_bad if time.time() - ts < 180]
    cf = is_challenge(page) or any(st in (403, 429, 503) for st in bad)
    return BotError("Created a game but never saw it in the API"
                    + (f" · API errors in the last 3 min: {bad[-6:]}" if bad else " · no API errors seen")
                    + (f" · page title '{page_title(page)}'" if cf else "")
                    + (" · treating this as a Cloudflare block (slow backoff retry)." if cf else
                       ". If 'viewer' is missing from the API response, set player_id in this account's config.json."),
                    shot(page), blocked=cf)


def give_up_window(sess, gid, why):
    dlog("YIELD", why=why)
    print("[game] another session already follows this game: closing the window", flush=True)
    close_game_window(sess.page)
    sess.listing = None
    raise Yielded(gid)


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
    cfg, cfg_at = get_cfg(), time.monotonic(); set_debug(cfg)
    diag_sent, tries = set(), {}
    last_iter, last_wait, last_click_done, late_key = time.monotonic(), 0.0, 0.0, None
    last_win_chk = 0.0
    last_st = None   # (status, turn): a flip (waiting -> active, intro -> first turn) restarts the "no change" clock
    nobtn, last_nobtn_log = None, 0.0   # (position key, first time the button was missing/disabled)
    dlog("GAMESTART", note="waiting for opponent")

    while True:
        if DEBUG:
            _now = time.monotonic()
            if _now - last_iter > 0.5:
                dlog("LOOPLAG", gap_ms=round((_now - last_iter) * 1000), load=os.getloadavg()[0])
            last_iter = _now
        if time.monotonic() - cfg_at > 20:
            cfg, cfg_at = get_cfg(), time.monotonic(); set_debug(cfg)
        if sess.dup and gid:
            give_up_window(sess, gid, f"{sess.dup} saw it first")
        while q:
            rgid, resp, arr = q.popleft()
            tj = time.monotonic()
            try:
                g = extract_game(resp.json())
            except Exception as e:
                dlog("JSONFAIL", err=type(e).__name__)
                continue
            if not g:
                continue
            if gid is None:
                if is_done(g) or my_seat(g, cfg) is None:
                    continue
                gid = rgid
                seen_ms = g.get("serverNow") or int(time.time() * 1000)
                if CSTORE is not None and duplicate_of(gid, seen_ms):   # same game as another session: later one yields
                    give_up_window(sess, gid, "claimed by another session")
                sess.listing, sess.created = gid, False
                if CSTORE is not None:
                    claim_async(sess, gid, seen_ms)
                print("[game] tracking" if IN_ACTIONS else f"[game] tracking {gid}", flush=True)
            elif rgid != gid:
                continue
            game, recv_at = g, time.time()
            st = (g.get("status"), g.get("turn"))
            if st != last_st:
                last_st, seen_at = st, time.time()
            seat = my_seat(g, cfg) or seat
            moves = sorted(g["moves"], key=lambda m: m.get("seq", 0))
            cols = [m["column"] for m in moves]
            k = gid + ":" + "".join(map(str, cols))
            if DEBUG:
                te_, sn_ = g.get("turnEndsAt"), g.get("serverNow")
                try:
                    span = (float(te_) - float(sn_)) / 1000
                    rem_arr = span - (time.time() - arr)
                    skew = float(sn_) - arr * 1000   # server clock minus ours at arrival (ms); big = clock offset
                except (TypeError, ValueError):
                    span = rem_arr = skew = None
                dlog("API", n=len(moves), new=int(k != key), turn=g.get("turn"), status=g.get("status"), starts_in_s=_f(None if g.get("turnStartsAt") is None or g.get("serverNow") is None else (g["turnStartsAt"] - g["serverNow"]) / 1000),
                     te_type=type(te_).__name__, sn_type=type(sn_).__name__, te_minus_sn_s=_f(span), rem_at_arrival_s=_f(rem_arr),
                     skew_ms=_f(skew, 0), proc_lag_ms=f"{(time.time() - arr) * 1000:.0f}", json_ms=f"{(time.monotonic() - tj) * 1000:.0f}")
            if k != key:
                key, seen_at, over_since = k, time.time(), None
                if DEBUG:
                    last = moves[-1] if moves else {}
                    dlog("NEWPOS", n=len(moves), last=f"{(last.get('seat') or '?')[:1].upper()}{last.get('column', -2) + 1}",
                         last_auto=int(bool(last.get("auto"))), seat=seat, timeouts=g.get("timeouts"))
                    if last.get("auto"):
                        dlog("AUTOMOVE", note="last move was auto-played by the site (turn timeout)", seat_of_move=last.get("seat"), mine=int(last.get("seat") == seat))
                    if last_click_done and last.get("seat") == seat:
                        dlog("ACK", own_move_visible_in_api_after_ms=round((time.monotonic() - last_click_done) * 1000))
                        last_click_done = 0.0
                req = solver.request(cols) if cols else None
            if g.get("opponentId") or moves:
                joined_at = joined_at or time.monotonic()
                sess.listing = None          # someone joined: nothing to cancel any more

        if game is None:
            waited = time.monotonic() - t_start
            if gid is None and waited > NO_WINDOW_S and time.monotonic() - last_win_chk > 1.0:
                last_win_chk = time.monotonic()
                if not window_open(page):
                    dlog("NOWINDOW", waited_s=_f(waited))
                    raise NoWindow()
            if gid is None and waited > 45:
                raise never_saw_error(sess)
            page.wait_for_timeout(200)
            continue

        moves = sorted(game["moves"], key=lambda m: m.get("seq", 0))
        n = len(moves)
        sol = solver.get(req) if req else {}

        if is_done(game):
            sess.listing = None
            dbg_end(game, seat, sol, "api-done")
            return game, seat, sol
        if sol.get("over"):
            over_since = over_since or time.monotonic()
            if time.monotonic() - over_since > 15:   # API never showed a final state: settle from the board
                dbg_end(game, seat, sol, "settled-from-board")
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
        if played_key == key and time.time() - played_at < float(cfg.get("retry_ms", 1500)) / 1000:
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
        if DEBUG and late and late_key != key:
            late_key = key
            dlog("LATE", rem_s=_f(rem), rem_is_none=int(rem is None), since_seen_ms=round((time.time() - seen_at) * 1000), sol=sol_sum(sol))

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
            if DEBUG and time.monotonic() - last_wait > 1.0:
                last_wait = time.monotonic()
                dlog("WAIT", n=n, rem_s=_f(rem), since_seen_ms=round((time.time() - seen_at) * 1000), sol=sol_sum(sol))
            page.wait_for_timeout(100)
            continue
        fallback = False
        if col < 0 or col not in legal:
            if not late:
                page.wait_for_timeout(100)
                continue
            col, fallback = legal[0], True

        t_dec = time.monotonic()
        if DEBUG:
            why = ("opening" if n == 0 else "forced" if len(legal) == 1 else "LATE-NO-SOLUTION-FALLBACK" if fallback
                   else "proven" if ready else "LATE-UNPROVEN-BEST-SO-FAR")
            dom_txt, cdp_rtt = dom_probe(page)
            dlog("DECIDE", why=why, col=col + 1, n=n, legal=[c + 1 for c in legal], rem_s=_f(rem), dom_timer=repr(dom_txt),
                 cdp_rtt_ms=round(cdp_rtt), since_seen_ms=round((time.time() - seen_at) * 1000),
                 load=os.getloadavg()[0], sol=sol_sum(sol))
        try:
            tm = {} if DEBUG else None
            status = attempt_click(page, col, cfg, tries.get(key, 0), seat, tm)
            if status == "ok":
                played_key, played_at = key, time.time()
                last_click_done = time.monotonic()
                nobtn = None
                if DEBUG:
                    dlog("CLICK", col=col + 1, decide_to_done_ms=round((last_click_done - t_dec) * 1000), **tm,
                         rem_after_s=_f(None if rem is None else rem - (last_click_done - t_dec)), try_no=tries.get(key, 0) + 1)
                tries[key] = tries.get(key, 0) + 1
                if tries[key] == 3 and "noeffect" not in diag_sent:
                    diag_sent.add("noeffect")
                    diag(page, game, seat, turn, col, "clicked this column 3 times but the position never changed")
            else:
                # not clicked: keep polling, never block
                if status in ("notmine", "over") or played_key == key:   # page shows opponent / game over / our click registered
                    page.wait_for_timeout(150)
                    continue
                if status == "slow" or str(status).startswith("err:"):    # browser too slow to answer: just try again
                    dlog("SLOWCLICK", status=status, col=col + 1, rem_s=_f(rem))
                    page.wait_for_timeout(50)
                    continue
                now_m = time.monotonic()
                if nobtn is None or nobtn[0] != key:
                    nobtn = (key, now_m)
                if DEBUG and now_m - last_nobtn_log > 1.0:
                    last_nobtn_log = now_m
                    dlog("NOBTN", col=col + 1, status=status, waited_ms=round((now_m - nobtn[1]) * 1000), rem_s=_f(rem),
                         eval_ms=(tm or {}).get("eval_ms"), rev=CODE_REV)
                if now_m - nobtn[1] > 8 and "nobtn" not in diag_sent:   # only report it if it persists
                    diag_sent.add("nobtn")
                    diag(page, game, seat, turn, col, "column button missing or disabled on our turn for 8+ s")
        except Exception as e:
            print("click failed:", type(e).__name__, flush=True)
            dlog("CLICKERR", err=type(e).__name__, msg=str(e)[:120].replace("\n", " "))
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
    cfg = get_cfg()           # read the config first: a disabled account must exit even without a login state
    set_debug(cfg)
    if not cfg["enabled"]:
        print("Disabled in config.json. Exiting.")
        return 0
    stats = load_stats()
    stats.setdefault("pnl", 0)
    stats.setdefault("rec", {"w": 0, "l": 0, "d": 0, "v": 0, "u": 0})
    src_hash = prepare_state()
    if stats.get("dead_hash") == src_hash:
        print("Login state is known to be logged out and has not been replaced. Exiting quietly.")
        return 0
    stats.pop("dead_hash", None)

    if cfg.get("reset_pnl_token") == stats.get("reset_token") and cfg["max_loss"] \
            and stats["pnl"] <= -parse_amount(cfg["max_loss"]):
        print("Max loss already reached (change reset_pnl_token or max_loss to continue). Exiting.")
        return 0
    solver = Solver()
    if DEBUG:
        try:
            node_v = subprocess.run(["node", "-v"], capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            node_v = "?"
        dlog("ENV", rev=CODE_REV, python=sys.version.split()[0], node=node_v, cpus=os.cpu_count(), load=os.getloadavg(), actions=int(IN_ACTIONS),
             safe_remaining_s=cfg["safe_remaining_s"], min_delay_ms=cfg["min_delay_ms"], solver_total_ms=7000, tt_bits=24)
    notify(f"▶️ {LABEL}Connect 4 bot started · {now_str()} · rev {CODE_REV}\nBooks: {solver.books}\n"
           f"Stake {fmt(parse_amount(cfg['stake']))} · seat {cfg['seat']} · P/L so far {fmt_signed(stats['pnl'])}")
    games = errors = 0
    blocked_n, blocked_since = 0, None
    limit_n = 0
    skip_reload = False
    no_win_n = 0

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
                set_debug(cfg)
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
                    if not lobby_ready and not skip_reload:
                        bal = open_lobby(sess)
                        if blocked_n:
                            notify(f"✅ Cloudflare cleared after {blocked_n} retries "
                                   f"({(time.monotonic() - blocked_since) / 60:.0f} min). Resuming.")
                            blocked_n, blocked_since = 0, None
                    lobby_ready = False
                    skip_reload = False
                    if bal < stake or (cfg["min_balance"] and bal < parse_amount(cfg["min_balance"])):
                        reason = f"balance {fmt(bal)} is below the stake/min_balance"; break

                    create_game(sess, stake)
                    try:
                        result = play_game(sess, solver, float(cfg["opponent_wait_s"]))
                    except Yielded as y:
                        # a window of a game another session plays was in our way: it is closed now, make our own game
                        if sess.created:   # our own listing is now waiting without a window: cancel unclaimed extras
                            reconcile_extras(sess, exclude={y.gid}, reload_first=False)
                        sess.created = False
                        dflush(f"yield {games + 1}")
                        skip_reload = True
                        continue
                    except NoWindow:
                        # no window / unique API after Create: count the lobby listings, cancel extras, then create again
                        sess.created = False
                        no_win_n += 1
                        if no_win_n >= 4:
                            no_win_n = 0
                            raise never_saw_error(sess)
                        reconcile_extras(sess, reload_first=True)
                        dflush(f"nowindow {games + 1}")
                        skip_reload = True
                        continue
                    finally:
                        claim_release_async()
                    dflush(f"after game {games + 1}")
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
                    record_game(stats, outcome, profit, unknown)
                    games += 1
                    errors = 0
                    limit_n = 0
                    no_win_n = 0

                    save_stats(stats)
                    sess.page.wait_for_timeout(int(PAYOUT_SETTLE_S * 1000))
                    try:
                        bal = open_lobby(sess)   # fresh load = true balance after payout
                        lobby_ready = True
                        stats["balance"], stats["balance_at"] = bal, datetime.now(timezone.utc).isoformat(timespec="seconds")
                        save_stats(stats)
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
                    if e.limit:
                        limit_n += 1
                        if limit_n == 1 or limit_n % 10 == 0:
                            notify(f"⏳ {LABEL}{e}. Waiting for a slot to free up; retrying about every minute.")
                        if not nap(random.uniform(45, 75)):
                            reason = "stopped while waiting for a free listing slot"
                            break
                        continue
                    if e.blocked:
                        blocked_n += 1
                        blocked_since = blocked_since or time.monotonic()
                        steps = cfg["cf_retry_minutes"] or [5]
                        wait_min = steps[min(blocked_n - 1, len(steps) - 1)] * random.uniform(0.9, 1.2)
                        blocked_for = (time.monotonic() - blocked_since) / 60
                        if blocked_for + wait_min > float(cfg["cf_give_up_min"]):
                            notify(f"🛑 {LABEL}Blocked by Cloudflare for {blocked_for:.0f} min. Giving up for this run; "
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
                    notify(f"❌ {LABEL}**Error** · {now_str()}\n{e}", e.shot)
                    if e.fatal:
                        stats["dead_hash"] = src_hash
                        notify(f"🔑 {LABEL}**Login state is dead. Reload it:** run `python login.py {(ACCOUNT + SFX) or '<name>'} --encrypt` on your PC "
                               "(Chrome open with --remote-debugging-port=9222 and logged in), then upload the new "
                               "`state.enc` to the private data repo. The bot stays quiet until the state changes.")
                        exit_code, reason = 2, "logged out"; break
                    errors += 1
                except Exception as e:
                    notify(f"❌ {LABEL}**Unexpected error** · {now_str()}\n{type(e).__name__}: {e}",
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
        dflush("exit")
        notify(f"⏹️ {LABEL}Connect 4 bot {reason} · games this run: {games} · P/L {fmt_signed(stats['pnl'])}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
