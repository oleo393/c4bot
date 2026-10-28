"""Encrypt an existing storage_state JSON and upload it (no Chrome needed).

  set STATE_KEY=...  set C4_DATA_REPO=owner/repo  set C4_DATA_TOKEN=...
  python upload_state.py storage_state_main.json          -> accounts/main/state.enc
  python upload_state.py storage_state_main-1.json        -> accounts/main/state-1.enc
(the session name is taken from the file name; pass it as a 2nd argument to override, e.g. `... alt-2`)
"""
import json, os, re, sys
if len(sys.argv) not in (2, 3):
    sys.exit("usage: python upload_state.py <storage_state_NAME.json> [NAME]")
m = re.search(r"storage_state_(.+)\.json$", os.path.basename(sys.argv[1]))
name = sys.argv[2] if len(sys.argv) == 3 else (m.group(1) if m else "")
if not name:
    sys.exit("Cannot tell the session name from the file name; pass it as a 2nd argument (e.g. main-1).")
account, _, slot = name.partition("-")
state_name = f"state-{slot}.enc" if slot else "state.enc"
os.environ.setdefault("C4_DATA_PREFIX", f"accounts/{account}")

from cryptography.fernet import Fernet
from store import Store
key = os.environ.get("STATE_KEY", "").strip()
if not key:
    sys.exit("Set STATE_KEY first (same value as the GitHub secret).")
st = Store()
if not st.enabled:
    sys.exit("Set C4_DATA_REPO (owner/repo, not a URL) and C4_DATA_TOKEN.")
if "/" not in st.repo or st.repo.startswith("http"):
    sys.exit(f"C4_DATA_REPO must look like owner/repo, got: {st.repo}")
with open(sys.argv[1], encoding="utf-8-sig") as f:
    state = json.load(f)
if not isinstance(state, dict) or "cookies" not in state:
    sys.exit("That file does not look like a Playwright storage_state (no 'cookies' key).")
blob = Fernet(key.encode()).encrypt(json.dumps(state).encode())
st.get(state_name)
st.put(state_name, blob, "new session")
print(f"Uploaded {st.prefix}/{state_name} to {st.repo}")
