"""Tiny client for a PRIVATE GitHub 'data' repo that holds config.json, state.enc and stats.enc.
The public code repo never contains any of those. Needs env C4_DATA_REPO ("user/repo") and
C4_DATA_TOKEN (fine-grained PAT, Contents: read & write on that one repo).

Multi-account: env C4_DATA_PREFIX (e.g. "accounts/alt") puts every path under that folder, or pass
Store(prefix="accounts/alt"). Store(prefix="") addresses the repo root (used for summary.json)."""
import base64, os, random, time
import requests


class Store:
    def __init__(self, prefix=None):
        self.repo = os.environ.get("C4_DATA_REPO", "").strip()
        self.tok = os.environ.get("C4_DATA_TOKEN", "").strip()
        self.enabled = bool(self.repo and self.tok)
        self.prefix = (os.environ.get("C4_DATA_PREFIX", "") if prefix is None else prefix).strip().strip("/")
        self._sha = {}

    def _path(self, path):
        return f"{self.prefix}/{path}" if self.prefix else path

    def _url(self, path):
        return f"https://api.github.com/repos/{self.repo}/contents/{self._path(path)}"

    def _h(self):
        return {"Authorization": f"Bearer {self.tok}", "Accept": "application/vnd.github+json"}

    def get(self, path):
        """-> bytes, or None if the file does not exist. Raises on other errors."""
        r = requests.get(self._url(path), headers=self._h(), timeout=20)
        if r.status_code == 404:
            self._sha.pop(path, None)
            return None
        r.raise_for_status()
        j = r.json()
        self._sha[path] = j["sha"]
        return base64.b64decode(j["content"])

    def list_files(self, path=""):
        """Names of the files directly inside `path`."""
        r = requests.get(self._url(path).rstrip("/"), headers=self._h(), timeout=20)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return sorted(e["name"] for e in r.json() if e.get("type") == "file")

    def list_dirs(self, path=""):
        """Names of the sub-folders of `path` (e.g. the account folders under accounts/)."""
        r = requests.get(self._url(path).rstrip("/"), headers=self._h(), timeout=20)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return sorted(e["name"] for e in r.json() if e.get("type") == "dir")

    def put_once(self, path, data, message="bot update"):
        """Single attempt (raises requests.HTTPError on 409/422 = somebody else wrote first). For read-merge-write loops."""
        body = {"message": message, "content": base64.b64encode(data).decode()}
        if self._sha.get(path):
            body["sha"] = self._sha[path]
        r = requests.put(self._url(path), headers=self._h(), json=body, timeout=30)
        r.raise_for_status()
        self._sha[path] = r.json()["content"]["sha"]

    def put(self, path, data, message="bot update"):
        """Create/update a file. Several accounts commit to the same repo at once, so a 409/422
        (stale sha / concurrent commit) is retried with a fresh sha and a short random delay."""
        for attempt in range(5):
            body = {"message": message, "content": base64.b64encode(data).decode()}
            if self._sha.get(path):
                body["sha"] = self._sha[path]
            r = requests.put(self._url(path), headers=self._h(), json=body, timeout=30)
            if r.status_code in (409, 422, 500, 502, 503) and attempt < 4:
                time.sleep(random.uniform(0.5, 2.0) * (attempt + 1))
                self._sha.pop(path, None)
                try:
                    self.get(path)
                except Exception:
                    pass
                continue
            r.raise_for_status()
            self._sha[path] = r.json()["content"]["sha"]
            return
