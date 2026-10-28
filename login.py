"""Save the logged-in browser session from your own Chrome (started with --remote-debugging-port=9222).

  python login.py main            -> storage_state_main.json      (the account's main session)
  python login.py main-1          -> storage_state_main-1.json    (extra session 1 of account "main")
  python login.py --genkey        -> print a new STATE_KEY (store it as a GitHub secret)
  python login.py main-1 --encrypt  -> also encrypt it (needs STATE_KEY) and upload to the private data repo as
                                     accounts/main/state-1.enc   (needs C4_DATA_REPO + C4_DATA_TOKEN)
Log in with a SEPARATE Chrome profile (--user-data-dir) for every session.
"""
import os, sys
from playwright.sync_api import sync_playwright

args = [a for a in sys.argv[1:] if not a.startswith("--")]
flags = {a for a in sys.argv[1:] if a.startswith("--")}

if "--genkey" in flags:
    from cryptography.fernet import Fernet
    print(Fernet.generate_key().decode())
    sys.exit(0)

name = args[0] if args else "main"
account, _, slot = name.partition("-")          # "main-1" -> account "main", slot "1"
out = f"storage_state_{name}.json"
state_name = f"state-{slot}.enc" if slot else "state.enc"
os.environ.setdefault("C4_DATA_PREFIX", f"accounts/{account}")   # Store reads this when it is created

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://localhost:9222")
    state = browser.contexts[0].storage_state(path=out)
    browser.close()
print("Saved", out)

if "--encrypt" in flags:
    from cryptography.fernet import Fernet
    import json
    key = os.environ.get("STATE_KEY", "").strip()
    if not key:
        sys.exit("Set the STATE_KEY environment variable first (same value as the GitHub secret).")
    blob = Fernet(key.encode()).encrypt(json.dumps(state).encode())
    from store import Store
    st = Store()
    if st.enabled:   # C4_DATA_REPO + C4_DATA_TOKEN set: upload straight to the private data repo
        if "/" not in st.repo or st.repo.startswith("http"):
            sys.exit(f"C4_DATA_REPO must look like owner/repo, got: {st.repo}")
        st.get(state_name)
        st.put(state_name, blob, "new session")
        print(f"Uploaded {st.prefix}/{state_name} to {st.repo}")
    else:
        with open(state_name, "wb") as f:
            f.write(blob)
        print(f"Wrote {state_name} locally. Upload it to your PRIVATE data repo (never the public one).")
