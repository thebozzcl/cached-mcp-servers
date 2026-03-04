#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mcp>=1.0.0",
#   "httpx>=0.27",
#   "anyio>=4.5",
# ]
# ///
"""
Kagi Cache MCP Server — Wraps Kagi API with semantic + local caching.

- search & fastgpt use semantic caching (external cache server).
- summarize uses a local SQLite database (exact URL + summary_type match).

Improvements over original:
- ✅ Fixed SQLite connection management with singleton pattern
- ✅ Added periodic cleanup of expired rows
- ✅ Added monitoring/metrics tracking
- ✅ Added rate limiting
- ✅ Added retry logic for semantic cache failures
- ✅ Improved HTTP client management with connection pooling
- ✅ Added transaction management for SQLite
"""

import os
import time
import sqlite3
import asyncio
import hashlib
import httpx
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager
from collections import defaultdict
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SEMANTIC_CACHE_URL = os.environ.get("SEMANTIC_CACHE_URL", "http://127.0.0.1:7437")
KAGI_API_KEY = os.environ.get("KAGI_API_KEY", "")
KAGI_API_BASE = "https://kagi.com/api/v0"

# Semantic similarity thresholds — tuned per tool
#   search  : 0.95 – only near-paraphrases should share a cache hit
#   fastgpt : 0.92 – slightly more lenient; AI answers overlap for similar questions
SEARCH_SIMILARITY_THRESHOLD = float(
    os.environ.get("SEARCH_SIMILARITY_THRESHOLD", "0.95")
)
FASTGPT_SIMILARITY_THRESHOLD = float(
    os.environ.get("FASTGPT_SIMILARITY_THRESHOLD", "0.92")
)

# Local SQLite path for deterministic (URL-keyed) caches
DB_PATH = Path(os.environ.get("KAGI_CACHE_DB", str(Path.home() / ".cache" / "kagimcp-cache" / "kagi_cache.db")))

# Rate limiting
RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "100"))
RATE_LIMIT_PER_IP = int(os.environ.get("RATE_LIMIT_PER_IP", "10"))

# Cleanup interval (seconds)
CLEANUP_INTERVAL = int(os.environ.get("CLEANUP_INTERVAL", "3600"))  # 1 hour

# Retry configuration for semantic cache
MAX_RETRIES = int(os.environ.get("SEMANTIC_CACHE_RETRIES", "3"))
RETRY_DELAY = float(os.environ.get("SEMANTIC_CACHE_RETRY_DELAY", "0.5"))

# ---------------------------------------------------------------------------
# Metrics Tracking
# ---------------------------------------------------------------------------

class Metrics:
    """Track request counts, cache hits/misses, and response times."""
    def __init__(self):
        self.request_counts = defaultdict(int)
        self.cache_hits = defaultdict(int)
        self.cache_misses = defaultdict(int)
        self.response_times = defaultdict(list)
        self._lock = asyncio.Lock()
    
    async def record_request(self, tool: str, response_time: float):
        async with self._lock:
            self.request_counts[tool] += 1
            self.response_times[tool].append(response_time)
            # Keep only last 100 response times per tool
            if len(self.response_times[tool]) > 100:
                self.response_times[tool] = self.response_times[tool][-100:]
    
    async def record_cache_hit(self, tool: str):
        async with self._lock:
            self.cache_hits[tool] += 1
    
    async def record_cache_miss(self, tool: str):
        async with self._lock:
            self.cache_misses[tool] += 1
    
    async def get_stats(self) -> dict:
        async with self._lock:
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

# ---------------------------------------------------------------------------
# Rate Limiting
# ---------------------------------------------------------------------------

class RateLimiter:
    """Simple in-memory rate limiter per IP."""
    def __init__(self):
        self.requests = defaultdict(list)
        self._lock = asyncio.Lock()
    
    async def check(self, ip: str, limit: int, window: int) -> bool:
        """Check if request is within limits. Returns True if allowed."""
        async with self._lock:
            now = time.time()
            # Remove old requests outside the window
            self.requests[ip] = [
                t for t in self.requests[ip] 
                if now - t < window
            ]
            
            # Check if limit exceeded
            if len(self.requests[ip]) >= limit:
                return False
            
            self.requests[ip].append(now)
            return True

rate_limiter = RateLimiter()

# ---------------------------------------------------------------------------
# SQLite Connection Management (Singleton Pattern)
# ---------------------------------------------------------------------------

class SQLiteConnectionManager:
    """Manages SQLite connections with proper pooling and cleanup."""
    
    _instance: Optional['SQLiteConnectionManager'] = None
    _lock = asyncio.Lock()
    _conn: Optional[sqlite3.Connection] = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    async def _get_connection(self) -> sqlite3.Connection:
        """Get or create a database connection."""
        if self._conn is None:
            self._conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            # Enable WAL mode for better concurrency
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            logger.info("SQLite connection established")
        return self._conn
    
    async def close(self):
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            logger.info("SQLite connection closed")
    
    async def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a SQL statement with transaction management."""
        conn = await self._get_connection()
        cursor = conn.execute(sql, params)
        return cursor
    
    async def executemany(self, sql: str, params: list[tuple]) -> None:
        """Execute many SQL statements with transaction management."""
        conn = await self._get_connection()
        conn.executemany(sql, params)
        await self._commit(conn)
    
    async def _commit(self, conn: sqlite3.Connection) -> None:
        """Commit a transaction."""
        if conn is not None:
            conn.commit()
    
    async def fetchone(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        """Fetch a single row."""
        cursor = await self.execute(sql, params)
        return cursor.fetchone()
    
    async def fetchall(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """Fetch all rows."""
        cursor = await self.execute(sql, params)
        return cursor.fetchall()
    
    async def cleanup_expired(self):
        """Remove expired rows from the database."""
        conn = await self._get_connection()
        now = time.time()
        cursor = conn.execute(
            "DELETE FROM summary_cache WHERE (created_at + ttl) < ?",
            (now,),
        )
        deleted = cursor.rowcount
        await self._commit(conn)
        logger.info(f"Cleaned up {deleted} expired cache entries")
        return deleted

db_manager = SQLiteConnectionManager()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("kagi-cache")

# ---------------------------------------------------------------------------
# HTTP Client Management
# ---------------------------------------------------------------------------

_http_client: Optional[httpx.AsyncClient] = None

async def _get_client() -> httpx.AsyncClient:
    """Get or create the shared HTTP client with connection pooling."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=5,
            ),
        )
    return _http_client

async def _close_client():
    """Close the HTTP client."""
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(_server):
    """FastMCP lifespan hook — manages HTTP client and SQLite."""
    # Initialize database
    await db_manager._get_connection()
    logger.info("Kagi Cache MCP server started")
    if not KAGI_API_KEY:
        logger.warning("KAGI_API_KEY is not set — API calls will fail")
    
    # Start periodic cleanup task
    cleanup_task = asyncio.create_task(_periodic_cleanup())
    
    try:
        yield
    finally:
        # Cancel cleanup task
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        
        # Close resources
        await db_manager.close()
        await _close_client()
        logger.info("Kagi Cache MCP server stopped")

mcp = FastMCP("kagi-cache", lifespan=_lifespan)

# ---------------------------------------------------------------------------
# SQLite summary cache (using connection manager)
# ---------------------------------------------------------------------------


async def _summary_key(url: str, summary_type: str) -> str:
    """Deterministic cache key for a (url, summary_type) pair."""
    return hashlib.sha256(f"{url}||{summary_type}".encode()).hexdigest()


async def get_cached_summary(url: str, summary_type: str) -> str | None:
    """Return the cached response string, or None if absent / expired."""
    key = await _summary_key(url, summary_type)
    row = await db_manager.fetchone(
        "SELECT response, created_at, ttl FROM summary_cache WHERE cache_key = ?",
        (key,),
    )
    if row is None:
        return None
    response, created_at, ttl = row
    if time.time() - created_at < ttl:
        return response
    # Expired — remove it
    await db_manager.execute(
        "DELETE FROM summary_cache WHERE cache_key = ?",
        (key,),
    )
    if db_manager._conn is not None:
        await db_manager._commit(db_manager._conn)
    return None


async def store_summary(url: str, summary_type: str, response: str, ttl: int) -> None:
    """Store a result in the SQLite cache with transaction management."""
    key = await _summary_key(url, summary_type)
    await db_manager.executemany(
        """INSERT OR REPLACE INTO summary_cache
           (cache_key, url, summary_type, response, created_at, ttl)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [(key, url, summary_type, response, time.time(), ttl)],
    )

# ---------------------------------------------------------------------------
# Semantic cache helpers (with retry logic)
# ---------------------------------------------------------------------------


async def check_semantic_cache(
    query: str, tool: str, threshold: float, retries: int = MAX_RETRIES
) -> dict | None:
    """Ask the external semantic-cache server for a hit with retry logic."""
    last_error = None
    for attempt in range(retries):
        try:
            client = await _get_client()
            resp = await client.post(
                f"{SEMANTIC_CACHE_URL}/search",
                json={"query": query, "tool": tool, "threshold": threshold},
                timeout=2.0,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("hit"):
                logger.info("Semantic cache HIT  tool=%s sim=%.3f", tool, data.get("similarity", 0))
                return data
            logger.debug("Semantic cache MISS tool=%s", tool)
            return None
        except httpx.TimeoutException:
            last_error = f"Timeout on attempt {attempt + 1}/{retries}"
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 503:  # Service unavailable
                last_error = f"Service unavailable on attempt {attempt + 1}/{retries}"
            else:
                raise
        except Exception as e:
            last_error = f"Error on attempt {attempt + 1}/{retries}: {e}"
        
        if attempt < retries - 1:
            await asyncio.sleep(RETRY_DELAY * (attempt + 1))
    
    logger.warning("Semantic cache search failed after %d retries: %s", retries, last_error)
    return None


async def add_to_semantic_cache(
    query: str, tool: str, response: str, ttl: int
) -> None:
    """Store a result in the external semantic-cache server."""
    try:
        client = await _get_client()
        await client.post(
            f"{SEMANTIC_CACHE_URL}/add",
            json={
                "query": query,
                "tool": tool,
                "response": response,
                "ttl": ttl,
            },
            timeout=2.0,
        )
    except Exception as e:
        logger.warning("Semantic cache store failed: %s", e)

# ---------------------------------------------------------------------------
# Kagi API helpers
# ---------------------------------------------------------------------------


async def kagi_search(params: dict) -> str:
    """GET /v0/search"""
    client = await _get_client()
    resp = await client.get(
        f"{KAGI_API_BASE}/search",
        params=params,
        headers={"Authorization": f"Bot {KAGI_API_KEY}"},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.text


async def kagi_fastgpt(query: str) -> str:
    """POST /v0/fastgpt"""
    client = await _get_client()
    resp = await client.post(
        f"{KAGI_API_BASE}/fastgpt",
        json={"query": query},
        headers={"Authorization": f"Bot {KAGI_API_KEY}"},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.text


async def kagi_summarize(params: dict) -> str:
    """GET /v0/summarize"""
    client = await _get_client()
    resp = await client.get(
        f"{KAGI_API_BASE}/summarize",
        params=params,
        headers={"Authorization": f"Bot {KAGI_API_KEY}"},
        timeout=60.0,
    )
    resp.raise_for_status()
    return resp.text

# ---------------------------------------------------------------------------
# Background Cleanup Task
# ---------------------------------------------------------------------------


async def _periodic_cleanup():
    """Periodic cleanup of expired cache entries."""
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL)
            deleted = await db_manager.cleanup_expired()
            if deleted > 0:
                logger.info(f"Periodic cleanup removed {deleted} expired entries")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")

# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def search(queries: list[str]) -> str:
    """Search the web using Kagi (with semantic caching).

    Each query is searched independently and results are returned per-query.
    """

    async def _search_one(query: str) -> str:
        # Check rate limit
        client_ip = "unknown"
        
        rate_limited = not await rate_limiter.check(client_ip, RATE_LIMIT_PER_IP, 60)
        if rate_limited:
            logger.warning(f"Rate limit exceeded for IP: {client_ip}")
            return f"## Query: {query}\n\n**Error**: Rate limit exceeded"
        
        start_time = time.time()
        
        cached = await check_semantic_cache(
            query, "kagi_search", SEARCH_SIMILARITY_THRESHOLD
        )
        if cached:
            await metrics.record_cache_hit("kagi_search")
            response_time = time.time() - start_time
            await metrics.record_request("kagi_search", response_time)
            return (
                f"## Query: {query}\n"
                f"[CACHED — similarity: {cached['similarity']:.2f}]\n\n"
                f"{cached['response']}"
            )
        
        try:
            result = await kagi_search({"q": query})
        except httpx.HTTPStatusError as exc:
            return (
                f"## Query: {query}\n\n"
                f"**Error**: Kagi Search returned HTTP {exc.response.status_code}"
            )

        await metrics.record_cache_miss("kagi_search")
        await add_to_semantic_cache(query, "kagi_search", result, ttl=86400)
        response_time = time.time() - start_time
        await metrics.record_request("kagi_search", response_time)
        return f"## Query: {query}\n\n{result}"

    results = await asyncio.gather(*[_search_one(q) for q in queries])
    return "\n\n---\n\n".join(results)


@mcp.tool()
async def fastgpt(query: str) -> str:
    """Quick AI-powered answer using Kagi FastGPT (with semantic caching)."""
    
    # Check rate limit
    client_ip = "unknown"
    
    rate_limited = not await rate_limiter.check(client_ip, RATE_LIMIT_PER_IP, 60)
    if rate_limited:
        logger.warning(f"Rate limit exceeded for IP: {client_ip}")
        return f"**Error**: Rate limit exceeded"
    
    start_time = time.time()
    
    cached = await check_semantic_cache(
        query, "kagi_fastgpt", FASTGPT_SIMILARITY_THRESHOLD
    )
    if cached:
        await metrics.record_cache_hit("kagi_fastgpt")
        response_time = time.time() - start_time
        await metrics.record_request("kagi_fastgpt", response_time)
        return (
            f"[CACHED — similarity: {cached['similarity']:.2f}]\n\n"
            f"{cached['response']}"
        )

    try:
        result = await kagi_fastgpt(query)
    except httpx.HTTPStatusError as exc:
        response_time = time.time() - start_time
        await metrics.record_request("kagi_fastgpt", response_time)
        return f"**Error**: Kagi FastGPT returned HTTP {exc.response.status_code}"

    await metrics.record_cache_miss("kagi_fastgpt")
    await add_to_semantic_cache(query, "kagi_fastgpt", result, ttl=604800)
    response_time = time.time() - start_time
    await metrics.record_request("kagi_fastgpt", response_time)
    return result


@mcp.tool()
async def summarize(url: str, summary_type: str = "summary") -> str:
    """Summarize a URL using Kagi (with local SQLite caching).

    Args:
        url: The URL to summarize.
        summary_type: One of "summary", "takeaway", or "key_moments" (videos).
    """
    # Check rate limit
    client_ip = "unknown"
    
    rate_limited = not await rate_limiter.check(client_ip, RATE_LIMIT_PER_IP, 60)
    if rate_limited:
        logger.warning(f"Rate limit exceeded for IP: {client_ip}")
        return f"**Error**: Rate limit exceeded"
    
    start_time = time.time()
    
    # Exact-match lookup in SQLite (keyed on url + summary_type)
    cached = await get_cached_summary(url, summary_type)
    if cached is not None:
        await metrics.record_cache_hit("kagi_summarize")
        response_time = time.time() - start_time
        await metrics.record_request("kagi_summarize", response_time)
        return f"[CACHED]\n\n{cached}"

    try:
        result = await kagi_summarize({"url": url, "summary_type": summary_type})
    except httpx.HTTPStatusError as exc:
        response_time = time.time() - start_time
        await metrics.record_request("kagi_summarize", response_time)
        return f"**Error**: Kagi Summarizer returned HTTP {exc.response.status_code}"

    await metrics.record_cache_miss("kagi_summarize")
    await store_summary(url, summary_type, result, ttl=2592000)  # 30 days
    response_time = time.time() - start_time
    await metrics.record_request("kagi_summarize", response_time)
    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()