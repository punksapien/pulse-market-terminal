"""ADR-0031: legacy cache tiers with explicit state/storage/path dependencies."""
import hashlib
import json
import tempfile
import time
from pathlib import Path


def bootstrap_cache(*, root):
    """Copy packaged volatility_cache directory to /tmp to prevent cold start timeouts on serverless functions."""
    ROOT = root
    try:
        import shutil
        src = ROOT / "volatility_cache"
        dst = Path(tempfile.gettempdir()) / "volatility_cache"
        if src.exists() and not dst.exists():
            print(f"[CACHE BOOTSTRAP] Copying packaged volatility_cache to {dst}...")
            shutil.copytree(str(src), str(dst), dirs_exist_ok=True)
            print("[CACHE BOOTSTRAP] Cache copied successfully!")
    except Exception as e:
        print(f"[CACHE BOOTSTRAP] Cache copy failed: {e}")


def load_precomputed_report(resolution, *, root):
    """Safely resolve and load the packaged precomputed report JSON for the given resolution."""
    ROOT = root
    filename = f"volatility_lw_report_{resolution}.json"
    paths = [
        (ROOT / "api") / filename,
        ROOT / filename,
        (ROOT / "api") / "volatility_lw_report.json",
        ROOT / "volatility_lw_report.json"
    ]
    for p in paths:
        if p.exists():
            try:
                return json.loads(p.read_text("utf-8"))
            except Exception:
                pass
    return None


def _tmp_cache_path(cache_key):
    digest = hashlib.sha256(str(cache_key).encode("utf-8")).hexdigest()[:24]
    return Path(tempfile.gettempdir()) / f"vol_signal_cache_{digest}.json"


def _try_serve_from_cache(cache_key, resolution, horizon, *, cache_state, storage, cache_ttl, cache_path):
    """Retrieve from L1 (memory) -> L2 (KV) -> L3 (/tmp). Returns report or None."""
    _CACHE = cache_state
    VercelStorage = storage
    CACHE_TTL = cache_ttl
    _tmp_cache_path = cache_path
    now = time.time()

    # L1: In-memory
    with _CACHE["lock"]:
        entry = _CACHE["store"].get(cache_key)
        if entry and (now - entry["ts"]) < CACHE_TTL:
            return entry["data"]

    # L2: Vercel KV
    if VercelStorage.is_available():
        try:
            cached_val = VercelStorage.get(cache_key)
            if cached_val:
                data = json.loads(cached_val)
                with _CACHE["lock"]:
                    _CACHE["store"][cache_key] = {"data": data, "ts": now}
                return data
        except Exception as e:
            print(f"[VERCEL STORAGE] Cache read failed: {e}")

    # L3: /tmp file cache
    tmp_path = _tmp_cache_path(cache_key)
    if tmp_path.exists():
        try:
            age = now - tmp_path.stat().st_mtime
            if age < CACHE_TTL:
                data = json.loads(tmp_path.read_text("utf-8"))
                with _CACHE["lock"]:
                    _CACHE["store"][cache_key] = {"data": data, "ts": now}
                return data
        except Exception:
            pass

    return None


def _serve_stale_or_fallback(cache_key, resolution, horizon, *, cache_state, storage, cache_path, fallback):
    """Serve any stale cached value or fallback to the precomputed JSON."""
    _CACHE = cache_state
    VercelStorage = storage
    _tmp_cache_path = cache_path
    load_precomputed_report = fallback
    # 1. Any memory cache
    with _CACHE["lock"]:
        entry = _CACHE["store"].get(cache_key)
        if entry:
            return entry["data"]

    # 2. Any Vercel KV cache
    if VercelStorage.is_available():
        try:
            cached_val = VercelStorage.get(cache_key)
            if cached_val:
                return json.loads(cached_val)
        except Exception:
            pass

    # 3. Any /tmp file cache
    tmp_path = _tmp_cache_path(cache_key)
    if tmp_path.exists():
        try:
            return json.loads(tmp_path.read_text("utf-8"))
        except Exception:
            pass

    # 4. Fallback to packaged report
    return load_precomputed_report(resolution)


def _save_to_local_cache_layers(cache_key, report, *, cache_state, cache_path):
    """Persist generated report into process memory and the instance-local /tmp."""
    _CACHE = cache_state
    _tmp_cache_path = cache_path
    now = time.time()
    with _CACHE["lock"]:
        _CACHE["store"][cache_key] = {"data": report, "ts": now}
    try:
        tmp_path = _tmp_cache_path(cache_key)
        tmp_path.write_text(json.dumps(report, separators=(",", ":")), "utf-8")
    except Exception:
        pass


def _save_to_cache_layers(cache_key, report, resolution, horizon, ex=180, *, save_local, storage):
    """Persist generated report across L1, L2, and L3 caches."""
    _save_to_local_cache_layers = save_local
    VercelStorage = storage
    _save_to_local_cache_layers(cache_key, report)
    # L2: Vercel KV
    if VercelStorage.is_available():
        try:
            VercelStorage.set(cache_key, json.dumps(report, separators=(",", ":")), ex=ex)
        except Exception as e:
            print(f"[CACHE LAYERS] Vercel KV save failed: {e}")
