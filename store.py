"""Tiny client for a PRIVATE GitHub 'data' repo that holds config.json, state.enc and stats.enc.
The public code repo never contains any of those. Needs env C4_DATA_REPO ("user/repo") and
C4_DATA_TOKEN (fine-grained PAT, Contents: read & write on that one repo)."""
import base64, os
import requests


class Store:
    def __init__(self):
        self.repo = os.environ.get("C4_DATA_REPO", "").strip()
        self.tok = os.environ.get("C4_DATA_TOKEN", "").strip()
        self.enabled = bool(self.repo and self.tok)
        self._sha = {}

    def _url(self, path):
        return f"https://api.github.com/repos/{self.repo}/contents/{path}"

    def _h(self):
        return {"Authorization": f"Bearer {self.tok}", "Accept": "application/vnd.github+json"}

    def get(self, path):
        """-> bytes, or None if the file does not exist. Raises on other errors."""
        r = requests.get(self._url(path), headers=self._h(), timeout=20)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        j = r.json()
        self._sha[path] = j["sha"]
        return base64.b64decode(j["content"])

    def put(self, path, data, message="bot update"):
        for attempt in range(2):
            body = {"message": message, "content": base64.b64encode(data).decode()}
            if self._sha.get(path):
                body["sha"] = self._sha[path]
            r = requests.put(self._url(path), headers=self._h(), json=body, timeout=30)
            if r.status_code in (409, 422) and attempt == 0:   # stale/missing sha: refresh and retry once
                self._sha.pop(path, None)
                try:
                    self.get(path)
                except Exception:
                    pass
                continue
            r.raise_for_status()
            self._sha[path] = r.json()["content"]["sha"]
            return
