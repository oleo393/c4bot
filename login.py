"""Save the logged-in browser session from your own Chrome (started with --remote-debugging-port=9222).

  python login.py               -> storage_state_main.json
  python login.py alt           -> storage_state_alt.json
  python login.py --genkey      -> print a new STATE_KEY (store it as a GitHub secret)
  python login.py --encrypt     -> also encrypt it (needs STATE_KEY) and upload to the private data repo
                                   (needs C4_DATA_REPO + C4_DATA_TOKEN), else write state.enc locally
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
out = f"storage_state_{name}.json"

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
        st.get("state.enc")
        st.put("state.enc", blob, "new session")
        print("Uploaded state.enc to the private data repo", st.repo)
    else:
        with open("state.enc", "wb") as f:
            f.write(blob)
        print("Wrote state.enc locally. Upload it to your PRIVATE data repo (never the public one).")
