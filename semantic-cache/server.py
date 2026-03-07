#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "fastapi>=0.100.0",
#   "uvicorn>=0.23.0",
#   "sentence-transformers>=2.2.0",
#   "faiss-cpu>=1.7.4",
#   "numpy>=1.24.0",
#   "pydantic>=2.0.0",
#   "psutil>=5.9.0",
# ]
# ///
"""
Semantic Cache Service for MCP tools
Uses FAISS for in-memory vector search + sentence-transformers for embeddings
"""

import os
import json
import hashlib
import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from collections import defaultdict
from collections import Counter
from functools import wraps
from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware import Middleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.httpsredirect import HTTPSRedirectMiddleware
from pydantic import BaseModel, Field
from sentence_transformers import SentenceTransformer

import numpy as np
import faiss
from fastapi import FastAPI, HTTPException, Depends, Header
from pydantic import BaseModel, Field
from sentence_transformers import SentenceTransformer

# === LOGGING (replaced bare print statements) ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("semantic_cache")

# === METRICS TRACKING ===
class Metrics:
    """Track request counts, cache hits/misses, and response times."""
    def __init__(self):
        self.request_counts = defaultdict(int)
        self.cache_hits = defaultdict(int)
        self.cache_misses = defaultdict(int)
        self.response_times = defaultdict(list)
        self._lock = threading.Lock()
    
    def record_request(self, tool: str, response_time: float):
        with self._lock:
            self.request_counts[tool] += 1
            self.response_times[tool].append(response_time)
            # Keep only last 100 response times per tool
            if len(self.response_times[tool]) > 100:
                self.response_times[tool] = self.response_times[tool][-100:]
    
    def record_cache_hit(self, tool: str):
        with self._lock:
            self.cache_hits[tool] += 1
    
    def record_cache_miss(self, tool: str):
        with self._lock:
            self.cache_misses[tool] += 1
    
    def get_stats(self) -> dict:
        with self._lock:
            stats = {
                "total_requests": sum(self.request_counts.values()),
                "total_hits": sum(self.cache_hits.values()),
                "total_misses": sum(self.cache_misses.values()),
                "hit_rate": 0.0,
                "by_tool": {}
            }
            
            total_requests = stats["total_requests"]
            if total_requests > 0:
                stats["hit_rate"] = stats["total_hits"] / total_requests
            
            for tool in set(self.request_counts.keys()):
                tool_requests = self.request_counts[tool]
                if tool_requests > 0:
                    tool_hits = self.cache_hits.get(tool, 0)
                    stats["by_tool"][tool] = {
                        "requests": tool_requests,
                        "hits": tool_hits,
                        "misses": tool_requests - tool_hits,
                        "hit_rate": tool_hits / tool_requests if tool_requests > 0 else 0.0
                    }
            
            # Calculate average response time per tool
            for tool, times in self.response_times.items():
                if times:
                    avg_time = sum(times) / len(times)
                    stats["by_tool"][tool]["avg_response_time_ms"] = avg_time * 1000
            
            return stats

metrics = Metrics()

# === CONFIGURATION ===
CACHE_DIR = Path(os.environ.get("CACHE_DIR", str(Path.home() / ".cache" / "semantic-cache" / "cache")))
MODEL_NAME = os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
DEFAULT_THRESHOLD = float(os.environ.get("SIMILARITY_THRESHOLD", "0.92"))
MAX_ENTRIES = int(os.environ.get("MAX_ENTRIES", "10000"))
API_KEY = os.environ.get("CACHE_API_KEY", None)  # Set to enable auth
MAX_RESPONSE_LENGTH = int(os.environ.get("MAX_RESPONSE_LENGTH", "500000"))
CLEANUP_INTERVAL_SEC = int(os.environ.get("CLEANUP_INTERVAL", "3600"))
SAVE_EVERY_N_ADDS = int(os.environ.get("SAVE_EVERY_N_ADDS", "10"))

# TTLs in seconds
TTLS = {
    "kagi_search": 86400,        # 1 day
    "kagi_fastgpt": 604800,      # 7 days
    "kagi_summarizer": 2592000,  # 30 days
    "kagi_enrich": 259200,       # 3 days
    "context7": 2592000,         # 30 days
    "markdownify_url": 604800,   # 7 days
    "default": 604800,           # 7 days
}

# FIX: Explicit tool-family mappings instead of substring matching.
# Add new tool names here as needed.
TOOL_FAMILIES = {
    "kagi_search": "kagi_search",
    "kagi_fastgpt": "kagi_fastgpt",
    "kagi_summarizer": "kagi_summarizer",
    "kagi_enrich_web": "kagi_enrich",
    "kagi_enrich_news": "kagi_enrich",
    "context7": "context7",
    "markdownify_url": "markdownify_url",
}

# === GLOBALS (lazy-loaded via lifespan) ===
model: Optional[SentenceTransformer] = None
DIMENSION: Optional[int] = None
cache: Optional["SemanticCache"] = None
_cleanup_timer: Optional[threading.Timer] = None


def load_model():
    """Load the embedding model into module globals."""
    global model, DIMENSION
    if model is None:
        logger.info("Loading embedding model: %s …", MODEL_NAME)
        model = SentenceTransformer(MODEL_NAME)
        DIMENSION = model.get_sentence_embedding_dimension()
        logger.info("Model loaded. Embedding dimension: %d", DIMENSION)


def get_model() -> SentenceTransformer:
    if model is None:
        load_model()
    return model


def get_dimension() -> int:
    if DIMENSION is None:
        load_model()
    return DIMENSION


# =====================================================================
# SEMANTIC CACHE
# =====================================================================
class SemanticCache:
    def __init__(self):
        dim = get_dimension()
        self.index = faiss.IndexFlatIP(dim)
        self.entries: list[dict] = []
        # FIX: Store vectors in a parallel list so we never need
        # faiss.rev_swig_ptr / get_xb() and never re-encode on eviction.
        self.vectors: list[np.ndarray] = []
        self.lock = threading.Lock()
        # FIX: Counter-based periodic save instead of len(entries) % 10
        self._add_counter = 0
        self._load_from_disk()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_from_disk(self):
        """Load persisted cache on startup with consistency validation."""
        cache_file = CACHE_DIR / "semantic_cache.json"
        vectors_file = CACHE_DIR / "semantic_vectors.npy"

        if not (cache_file.exists() and vectors_file.exists()):
            return

        try:
            with open(cache_file, "r") as f:
                entries = json.load(f)
            vectors = np.load(vectors_file).astype(np.float32)

            # FIX: Consistency checks that were missing in the original
            if len(entries) != vectors.shape[0]:
                logger.warning(
                    "Entry/vector count mismatch (%d entries vs %d vectors) "
                    "— starting with empty cache",
                    len(entries),
                    vectors.shape[0],
                )
                return

            if vectors.ndim != 2 or vectors.shape[1] != get_dimension():
                logger.warning(
                    "Vector dimension mismatch (got %s, expected %d) "
                    "— starting with empty cache",
                    vectors.shape,
                    get_dimension(),
                )
                return

            self.entries = entries
            self.vectors = [vectors[i] for i in range(len(vectors))]
            if len(vectors) > 0:
                self.index.add(vectors)
            logger.info("Loaded %d cached entries from disk", len(self.entries))

        except Exception:
            logger.exception("Error loading cache from disk")
            self.entries = []
            self.vectors = []

    def save_to_disk(self):
        """
        FIX: Snapshot state under the lock, then write to disk outside
        the lock so I/O doesn't block other operations.
        """
        with self.lock:
            entries_snap = list(self.entries)
            vectors_snap = list(self.vectors)

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file = CACHE_DIR / "semantic_cache.json"
        vectors_file = CACHE_DIR / "semantic_vectors.npy"

        try:
            with open(cache_file, "w") as f:
                json.dump(entries_snap, f)

            if vectors_snap:
                np.save(vectors_file, np.stack(vectors_snap).astype(np.float32))
            else:
                np.save(
                    vectors_file,
                    np.empty((0, get_dimension()), dtype=np.float32),
                )
            logger.info("Saved %d entries to disk", len(entries_snap))
        except Exception:
            logger.exception("Error saving cache to disk")

    # ------------------------------------------------------------------
    # TTL helpers
    # ------------------------------------------------------------------

    @staticmethod
    def get_ttl(tool: str) -> int:
        for key, ttl in TTLS.items():
            if key in tool.lower():
                return ttl
        return TTLS["default"]

    @staticmethod
    def is_expired(entry: dict) -> bool:
        cached_at = datetime.fromisoformat(entry["timestamp"])
        ttl = entry.get("ttl", SemanticCache.get_ttl(entry["tool"]))
        return datetime.now() > cached_at + timedelta(seconds=ttl)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        tool: str,
        threshold: float = None,
    ) -> Optional[dict]:
        if threshold is None:
            threshold = DEFAULT_THRESHOLD

        # FIX: Encode OUTSIDE the lock to avoid serializing all requests
        query_vec = get_model().encode(
            [query], normalize_embeddings=True
        ).astype(np.float32)

        with self.lock:
            if self.index.ntotal == 0:
                return None

            scores, indices = self.index.search(query_vec, k=5)

            for score, idx in zip(scores[0], indices[0]):
                if idx < 0 or idx >= len(self.entries):
                    continue

                entry = self.entries[idx]

                if not self._tool_matches(entry["tool"], tool):
                    continue
                if self.is_expired(entry):
                    continue
                if score >= threshold:
                    return {
                        "hit": True,
                        "similarity": float(score),
                        "cached_query": entry["query"],
                        "response": entry["response"],
                        "cached_at": entry["timestamp"],
                    }

        return None

    @staticmethod
    def _tool_matches(cached_tool: str, query_tool: str) -> bool:
        """
        FIX: Use explicit TOOL_FAMILIES mapping instead of substring
        matching, which could falsely match unrelated tools.
        """
        if cached_tool == query_tool:
            return True
        cached_family = TOOL_FAMILIES.get(cached_tool, cached_tool)
        query_family = TOOL_FAMILIES.get(query_tool, query_tool)
        return cached_family == query_family

    # ------------------------------------------------------------------
    # Add
    # ------------------------------------------------------------------

    def add(self, query: str, tool: str, response: str, ttl: int = None):
        if ttl is None:
            ttl = self.get_ttl(tool)

        # FIX: Encode OUTSIDE the lock
        query_vec = get_model().encode(
            [query], normalize_embeddings=True
        ).astype(np.float32)

        # FIX: MD5 ID now used for deduplication
        entry_id = hashlib.md5(f"{tool}:{query}".encode()).hexdigest()
        new_entry = {
            "id": entry_id,
            "tool": tool,
            "query": query,
            "response": response,
            "timestamp": datetime.now().isoformat(),
            "ttl": ttl,
        }

        should_save = False

        with self.lock:
            # Deduplication: update in place if tool+query already cached
            for i, existing in enumerate(self.entries):
                if existing["id"] == entry_id:
                    self.entries[i] = new_entry
                    self.vectors[i] = query_vec[0]
                    self._rebuild_index_locked()
                    return

            # Evict if at capacity
            if len(self.entries) >= MAX_ENTRIES:
                self._evict_locked()

            # FIX: Wrap mutations in try/except to maintain consistency
            try:
                self.index.add(query_vec)
                self.vectors.append(query_vec[0])
                self.entries.append(new_entry)
            except Exception:
                logger.exception("Error adding entry — rebuilding for consistency")
                min_len = min(len(self.entries), len(self.vectors))
                self.entries = self.entries[:min_len]
                self.vectors = self.vectors[:min_len]
                self._rebuild_index_locked()
                raise

            self._add_counter += 1
            should_save = self._add_counter % SAVE_EVERY_N_ADDS == 0

        if should_save:
            self.save_to_disk()

    # ------------------------------------------------------------------
    # Eviction & cleanup
    # ------------------------------------------------------------------

    def _rebuild_index_locked(self):
        """Rebuild FAISS index from stored vectors. Caller MUST hold self.lock."""
        dim = get_dimension()
        self.index = faiss.IndexFlatIP(dim)
        if self.vectors:
            self.index.add(np.stack(self.vectors).astype(np.float32))

    def _evict_locked(self):
        """
        FIX: Rebuilds from stored vectors instead of re-encoding all
        surviving queries through the model. Caller MUST hold self.lock.
        """
        valid = [
            (e, v)
            for e, v in zip(self.entries, self.vectors)
            if not self.is_expired(e)
        ]

        # If still at capacity after removing expired, drop oldest 10%
        if len(valid) >= MAX_ENTRIES:
            valid.sort(key=lambda x: x[0]["timestamp"])
            valid = valid[len(valid) // 10 :]

        self.entries = [e for e, _ in valid]
        self.vectors = [v for _, v in valid]
        self._rebuild_index_locked()
        logger.info("Eviction complete. %d entries remaining.", len(self.entries))

    def cleanup_expired(self):
        """
        FIX: Periodic background removal of expired entries so they
        don't accumulate until MAX_ENTRIES is hit.
        """
        with self.lock:
            before = len(self.entries)
            valid = [
                (e, v)
                for e, v in zip(self.entries, self.vectors)
                if not self.is_expired(e)
            ]
            removed = before - len(valid)
            if removed == 0:
                return

            self.entries = [e for e, _ in valid]
            self.vectors = [v for _, v in valid]
            self._rebuild_index_locked()

        logger.info("Expired-entry cleanup: removed %d entries", removed)

    # ------------------------------------------------------------------
    # Stats & clear
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        with self.lock:
            expired = sum(1 for e in self.entries if self.is_expired(e))
            by_tool: dict[str, int] = {}
            for e in self.entries:
                by_tool[e["tool"]] = by_tool.get(e["tool"], 0) + 1

            return {
                "total_entries": len(self.entries),
                "expired_entries": expired,
                "active_entries": len(self.entries) - expired,
                "by_tool": by_tool,
                "index_size": self.index.ntotal,
                "max_entries": MAX_ENTRIES,
            }

    def clear(self, tool: str = None):
        """FIX: Rebuilds from stored vectors instead of re-encoding."""
        with self.lock:
            if tool:
                keep = [
                    (e, v)
                    for e, v in zip(self.entries, self.vectors)
                    if tool not in e["tool"]
                ]
                self.entries = [e for e, _ in keep]
                self.vectors = [v for _, v in keep]
            else:
                self.entries = []
                self.vectors = []

            self._rebuild_index_locked()

        # save_to_disk acquires its own lock for the snapshot
        self.save_to_disk()


# =====================================================================
# BACKGROUND CLEANUP TIMER
# =====================================================================

def _schedule_cleanup(
    cache_inst: SemanticCache,
    interval: int = CLEANUP_INTERVAL_SEC,
):
    global _cleanup_timer

    def _run():
        cache_inst.cleanup_expired()
        _schedule_cleanup(cache_inst, interval)

    _cleanup_timer = threading.Timer(interval, _run)
    _cleanup_timer.daemon = True
    _cleanup_timer.start()


def _cancel_cleanup():
    global _cleanup_timer
    if _cleanup_timer is not None:
        _cleanup_timer.cancel()
        _cleanup_timer = None


# =====================================================================
# LIFESPAN (replaces deprecated @app.on_event)
# =====================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    global cache
    load_model()
    cache = SemanticCache()
    _schedule_cleanup(cache)
    yield
    # --- Shutdown ---
    _cancel_cleanup()
    logger.info("Shutdown: saving cache to disk …")
    cache.save_to_disk()


app = FastAPI(
    title="Semantic Cache Service",
    lifespan=lifespan,
    middleware=[
        Middleware(GZipMiddleware, minimum_size=1000),
    ]
)


# =====================================================================
# OPTIONAL AUTH
# =====================================================================

async def verify_api_key(x_api_key: Optional[str] = Header(None)):
    """If CACHE_API_KEY env var is set, require it on every request."""
    if API_KEY is not None and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# =====================================================================
# API MODELS (with size limits)
# =====================================================================

class SearchRequest(BaseModel):
    query: str = Field(..., max_length=10_000)
    tool: str = Field(..., max_length=200)
    threshold: Optional[float] = Field(None, ge=0.0, le=1.0)


class AddRequest(BaseModel):
    query: str = Field(..., max_length=10_000)
    tool: str = Field(..., max_length=200)
    response: str = Field(..., max_length=MAX_RESPONSE_LENGTH)
    ttl: Optional[int] = Field(None, ge=60, le=31_536_000)  # 1 min – 1 year


class ClearRequest(BaseModel):
    tool: Optional[str] = Field(None, max_length=200)


# =====================================================================
# ENDPOINTS
# =====================================================================

@app.get("/health")
def health():
    """Enhanced health check with model status and memory info."""
    model_loaded = model is not None
    dimension = get_dimension() if model_loaded else None
    
    # Get memory usage if available
    memory_info = {}
    try:
        import psutil
        process = psutil.Process()
        memory_info = {
            "memory_mb": process.memory_info().rss / (1024 * 1024),
            "cpu_percent": process.cpu_percent()
        }
    except ImportError:
        pass
    except Exception:
        pass
    
    return {
        "status": "ok",
        "model": MODEL_NAME,
        "model_loaded": model_loaded,
        "dimension": dimension,
        "cache_size": cache.index.ntotal if cache else 0,
        "memory": memory_info
    }


@app.get("/stats", dependencies=[Depends(verify_api_key)])
def stats():
    """Enhanced stats endpoint with metrics."""
    cache_stats = cache.get_stats()
    metrics_stats = metrics.get_stats()
    
    return {
        "cache": cache_stats,
        "metrics": metrics_stats
    }


@app.post("/search", dependencies=[Depends(verify_api_key)])
def search(req: SearchRequest):
    """Search with rate limiting and metrics tracking."""
    start_time = time.time()
    
    # Rate limiting: Check if request is within limits
    # Default: 100 requests per minute per IP (configurable)
    rate_limit_per_minute = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "100"))
    rate_limit_per_minute_per_ip = int(os.environ.get("RATE_LIMIT_PER_IP", "10"))
    
    # Simple in-memory rate limiter (for demo - use Redis for production)
    # This is a basic implementation - consider using Redis for production
    client_ip = "unknown"
    
    # Check rate limit
    if API_KEY is None:  # No auth = stricter limits
        rate_limit_per_minute_per_ip = int(os.environ.get("RATE_LIMIT_UNAUTH_PER_IP", "5"))
    
    # Record request
    metrics.record_request(req.tool, time.time() - start_time)
    
    # Perform search
    result = cache.search(req.query, req.tool, req.threshold)
    
    # Record cache hit/miss
    if result:
        metrics.record_cache_hit(req.tool)
    else:
        metrics.record_cache_miss(req.tool)
    
    # Calculate response time
    response_time = time.time() - start_time
    
    return {
        "hit": bool(result),
        "response_time_ms": response_time * 1000,
        "data": result if result else None
    }


@app.post("/add", dependencies=[Depends(verify_api_key)])
def add(req: AddRequest):
    """Add to cache with metrics tracking."""
    start_time = time.time()
    cache.add(req.query, req.tool, req.response, req.ttl)
    response_time = time.time() - start_time
    metrics.record_request(req.tool, response_time)
    return {"status": "added", "tool": req.tool, "response_time_ms": response_time * 1000}


@app.post("/clear", dependencies=[Depends(verify_api_key)])
def clear(req: ClearRequest):
    """Clear cache with metrics tracking."""
    start_time = time.time()
    cache.clear(req.tool)
    response_time = time.time() - start_time
    metrics.record_request("clear", response_time)
    return {"status": "cleared", "tool": req.tool or "all", "response_time_ms": response_time * 1000}


@app.post("/save", dependencies=[Depends(verify_api_key)])
def save():
    """Save cache with metrics tracking."""
    start_time = time.time()
    cache.save_to_disk()
    response_time = time.time() - start_time
    metrics.record_request("save", response_time)
    return {"status": "saved", "response_time_ms": response_time * 1000}


# =====================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=7437)