"""OpenViking-backed knowledge store with seamless local fallback.

Design goals (meeting real-time path):
- Same interface as ``KnowledgeStore`` — pipeline/app code needs no changes.
- OV retrieval runs over the local HTTP server (127.0.0.1:1933) with hard
  socket timeouts; find() uses the *local* embedding (Ollama), so the
  real-time path has zero external API dependency.
- Any OV slowness/failure degrades to the built-in keyword store within
  ``ov_timeout_ms``; a circuit breaker skips OV entirely for a cooldown
  after repeated failures, so meeting replies never stall.
- The inner local store is always kept fresh: it backs ASR hotword
  extraction (``chunks``) and serves as the permanent fallback.
"""

import json
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

from server.rag.store import KnowledgeStore, _tokens


class OvKnowledgeStore:
    """Drop-in wrapper: OpenViking semantic retrieval + local IDF fallback."""

    def __init__(self, root: Path, config: dict):
        self.root = root
        self.local = KnowledgeStore(root, config.get("kb_dir", "docs/kb"))
        # OV settings (live-updatable via apply_config)
        self.enabled = False
        self.base_url = "http://127.0.0.1:1933"
        self.timeout_ms = 800
        self.target_uri = "viking://resources"
        self.apply_config(config)
        # Circuit breaker state
        self._lock = threading.Lock()
        self._failures = 0
        self._down_until = 0.0
        self._health_cache: tuple[float, bool] = (0.0, False)
        # Warm prefetch cache: key -> (ts, hits)
        self.warm_cache: dict[str, tuple[float, list[dict]]] = {}
        self.warm_ttl = 60.0

    # ------------------------------------------------------------------ config
    def apply_config(self, config: dict) -> None:
        self.enabled = bool(config.get("ov_enabled", False))
        self.base_url = str(config.get("ov_url", "http://127.0.0.1:1933")).rstrip("/")
        self.timeout_ms = int(config.get("ov_timeout_ms", 800))
        self.target_uri = str(config.get("ov_target_uri", "viking://resources"))

    # Attribute proxying: hotword extraction reads ``chunks``; the settings
    # endpoint writes kb_dir/chunks/_tokens when kb_dir changes.
    def __getattr__(self, name: str):
        # Only called for attributes not found on self — delegate to local store.
        return getattr(self.__dict__["local"], name)

    def __setattr__(self, name: str, value):
        if name in {"kb_dir", "chunks", "_tokens", "_df"} and "local" in self.__dict__:
            setattr(self.__dict__["local"], name, value)
        else:
            object.__setattr__(self, name, value)

    # ------------------------------------------------------------------- HTTP
    def _http(self, method: str, path: str, payload: dict | None = None,
              params: dict | None = None, timeout: float = 1.5) -> dict:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if body.get("status") == "error":
            raise RuntimeError(str(body.get("error", "openviking error")))
        return body.get("result", body)

    # --------------------------------------------------------- circuit breaker
    def _ov_available(self) -> bool:
        if not self.enabled:
            return False
        now = time.time()
        with self._lock:
            if now < self._down_until:
                return False
            cached_ts, cached_ok = self._health_cache
            if now - cached_ts < 5.0:
                return cached_ok
        try:
            self._http("GET", "/health", timeout=1.5)
            ok = True
        except Exception:  # noqa: BLE001 - any connection problem means "down"
            ok = False
        with self._lock:
            self._health_cache = (now, ok)
        return ok

    def _record_success(self) -> None:
        with self._lock:
            self._failures = 0

    def _record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= 3:
                # Open the circuit: skip OV for 60s so replies stay fast.
                self._down_until = time.time() + 60.0
                self._failures = 0
                self._health_cache = (0.0, False)

    # ------------------------------------------------------------------ OV ops
    def _ov_find(self, query: str, k: int) -> list[dict]:
        """find() + read() mapped to the local hit shape. Raises on failure."""
        budget = max(0.2, self.timeout_ms / 1000.0)
        result = self._http("POST", "/api/v1/search/find",
                            payload={"query": query, "target_uri": self.target_uri,
                                     "limit": max(k * 2, 8)},
                            timeout=budget)
        resources = result.get("resources", []) if isinstance(result, dict) else []
        # L2 = real files; skip L0 directory abstracts
        picked = [r for r in resources
                  if r.get("level") == 2 and ".abstract" not in str(r.get("uri", ""))][:k]
        if not picked:
            return []
        hits: list[dict] = []
        read_budget = max(0.2, budget / 2)
        for item in picked:
            uri = str(item.get("uri", ""))
            try:
                content = self._http("GET", "/api/v1/content/read",
                                     params={"uri": uri}, timeout=read_budget)
            except Exception:  # noqa: BLE001 - skip unreadable entries
                continue
            if isinstance(content, dict):
                text = str(content.get("content", ""))
            else:
                text = str(content)
            if not text.strip():
                continue
            rel = uri.split("://", 1)[-1]
            for prefix in ("resources/",):
                if rel.startswith(prefix):
                    rel = rel[len(prefix):]
            hits.append({
                "source": rel,
                "title": Path(rel).stem,
                "heading": "",
                "text": text[:1500],
                "score": round(float(item.get("score", 0.0)), 3),
                "backend": "openviking",
            })
        return hits

    def prewarm(self) -> None:
        """Warm the embedding model with a trivial query (kills cold-start spike).

        Called when a meeting session starts; first find() after an OV server
        restart pays ~1.2s model load, later ones cost tens of ms.
        """
        if not self._ov_available():
            return
        try:
            self._http("POST", "/api/v1/search/find",
                       payload={"query": "预热", "target_uri": self.target_uri,
                                "limit": 1},
                       timeout=10.0)
            self._record_success()
        except Exception:  # noqa: BLE001 - prewarm must never block a meeting
            self._record_failure()

    # ------------------------------------------------------------------ search
    def search(self, query: str, k: int = 4) -> list[dict]:
        if self.enabled and query.strip() and self._ov_available():
            try:
                hits = self._ov_find(query, k)
                self._record_success()
                if hits:
                    return hits
                # OV found nothing usable — keyword index may still match.
            except Exception:  # noqa: BLE001 - degrade, never stall a reply
                self._record_failure()
        return self.local.search(query, k)

    def search_expanded(self, query: str, k: int = 6) -> list[dict]:
        return self.local.search_expanded(query, k)

    # ------------------------------------------------------------ warm prefetch
    def warm(self, text: str, k: int = 3) -> list[dict]:
        hits = self.search(text, k)
        if hits:
            key = text.strip()[:80]
            self.warm_cache[key] = (time.time(), hits)
            now = time.time()
            self.warm_cache = {k_: v for k_, v in self.warm_cache.items()
                               if now - v[0] < self.warm_ttl * 4}
        return hits

    def take_warm(self, text: str) -> list[dict] | None:
        now = time.time()
        best_key = None
        best_hits = None
        needle = text.strip()[:80]
        for key, (ts, hits) in self.warm_cache.items():
            if now - ts > self.warm_ttl:
                continue
            if key and (key in needle or needle in key or
                        len(_tokens(key) & _tokens(needle)) >= 2):
                if best_key is None or ts > self.warm_cache[best_key][0]:
                    best_key, best_hits = key, hits
        if best_key is not None:
            del self.warm_cache[best_key]
            return best_hits
        return self.local.take_warm(text)

    # ------------------------------------------------------------------ ingest
    def _ov_upload_dir(self, directory: Path) -> str:
        """Zip a directory and upload it as a temp file; returns temp_file_id.

        The OV server rejects direct local filesystem paths (403 by design —
        arbitrary server-side reads are disallowed), so imports must go
        through the temp_upload channel, same as the official SDK.
        """
        zip_path = Path(tempfile.gettempdir()) / f"ov_kb_{uuid.uuid4().hex}.zip"
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                for file_path in sorted(directory.rglob("*")):
                    if file_path.is_file() and not file_path.is_symlink():
                        zipf.write(file_path, arcname=str(file_path.relative_to(directory)))
            boundary = uuid.uuid4().hex
            body = b"\r\n".join([
                f"--{boundary}".encode(),
                f'Content-Disposition: form-data; name="file"; filename="{zip_path.name}"'.encode(),
                b"Content-Type: application/octet-stream",
                b"",
                zip_path.read_bytes(),
                f"--{boundary}--".encode(),
                b"",
            ])
            req = urllib.request.Request(
                self.base_url + "/api/v1/resources/temp_upload", data=body, method="POST")
            req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
            with urllib.request.urlopen(req, timeout=60.0) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            if result.get("status") == "error":
                raise RuntimeError(str(result.get("error", "upload failed")))
            return str(result.get("result", {}).get("temp_file_id", ""))
        finally:
            zip_path.unlink(missing_ok=True)

    def ingest(self) -> dict:
        """Rebuild local index (must succeed), then best-effort OV re-sync."""
        stats = self.local.ingest()
        stats["ov"] = "disabled"
        if not self.enabled:
            return stats
        if not self._ov_available():
            stats["ov"] = "unavailable"
            return stats
        try:
            name = self.local.kb_dir.name
            # Idempotent re-import: drop the old kb resource tree first.
            try:
                self._http("DELETE", "/api/v1/fs",
                           params={"uri": f"{self.target_uri}/{name}",
                                   "recursive": "true"},
                           timeout=30.0)
            except Exception:  # noqa: BLE001 - fine if it never existed
                pass
            temp_file_id = self._ov_upload_dir(self.local.kb_dir)
            self._http("POST", "/api/v1/resources",
                       payload={"temp_file_id": temp_file_id, "source_name": name,
                                "wait": True, "timeout": 280},
                       timeout=300.0)
            self._record_success()
            stats["ov"] = "synced"
        except Exception as exc:  # noqa: BLE001
            self._record_failure()
            stats["ov"] = f"failed: {exc}"
        return stats

    # ------------------------------------------------------------------- stats
    def stats(self) -> dict:
        stats = self.local.stats()
        with self._lock:
            down = time.time() < self._down_until
            failures = self._failures
        stats["ov"] = {
            "enabled": self.enabled,
            "available": self.enabled and not down and self._ov_available(),
            "consecutive_failures": failures,
        }
        stats["warm_entries"] += len(self.warm_cache)
        return stats
