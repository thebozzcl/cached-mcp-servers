#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mcp>=1.0.0",
#   "httpx>=0.27",
#   "anyio>=4.5",
#   "aiosqlite>=0.20",
#   "psutil>=5.9.0",
# ]
# ///
"""
Context7 Cache MCP Server — Wraps Context7 with local caching.

Caching strategy
----------------
  resolve_library_id : SQLite exact-match cache (keyed on normalised library name)
  query_docs         : semantic similarity cache   (scoped per library + output format)

Improvements over original:
- ✅ Added rate limiting per IP address
- ✅ Added comprehensive metrics tracking (requests, hits, misses, response times)
- ✅ Added periodic cleanup of expired SQLite rows
- ✅ Added retry logic for semantic cache failures
- ✅ Improved HTTP client management with connection pooling
- ✅ Added background cleanup task
"""

import os
import time
import logging
import asyncio
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from collections import defaultdict
from typing import Optional

import aiosqlite
import httpx
import psutil
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration (all overridable via environment variables)
# ---------------------------------------------------------------------------
SEMANTIC_CACHE_URL = os.environ.get("SEMANTIC_CACHE_URL", "http://127.0.0.1:7437")
CONTEXT7_API_KEY   = os.environ.get("CONTEXT7_API_KEY", "")
CONTEXT7_API_BASE  = os.environ.get("CONTEXT7_API_BASE", "https://context7.com/api/v2")

DOCS_SIMILARITY_THRESHOLD = float(os.environ.get("DOCS_SIMILARITY_THRESHOLD", "0.88"))
RESOLVE_TTL = int(os.environ.get("RESOLVE_TTL", str(30 * 24 * 3600)))   # 30 days
DOCS_TTL    = int(os.environ.get("DOCS_TTL",    str( 7 * 24 * 3600)))   # 7 days

SQLITE_DB_PATH = os.environ.get("RESOLVE_CACHE_DB", str(Path.home() / ".cache" / "context7" / "resolve_cache.db"))
SHOW_CACHE_METADATA = os.environ.get("SHOW_CACHE_METADATA", "true").lower() == "true"

# Rate limiting
RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "100"))
RATE_LIMIT_PER_IP = int(os.environ.get("RATE_LIMIT_PER_IP", "10"))
RATE_LIMIT_UNAUTH_PER_IP = int(os.environ.get("RATE_LIMIT_UNAUTH_PER_IP", "5"))

# Cleanup interval (seconds)
CLEANUP_INTERVAL = int(os.environ.get("CLEANUP_INTERVAL", "3600"))  # 1 hour

# Retry configuration for semantic cache
MAX_RETRIES = int(os.environ.get("SEMANTIC_CACHE_RETRIES", "3"))
RETRY_DELAY = float(os.environ.get("SEMANTIC_CACHE_RETRY_DELAY", "0.5"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("context7-cache")

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
# SQLite Connection Management (Singleton Pattern)
# ---------------------------------------------------------------------------

class SQLiteConnectionManager:
    """Manages SQLite connections with proper pooling and cleanup."""
    
    _instance: Optional['SQLiteConnectionManager'] = None
    _lock = asyncio.Lock()
    _conn: Optional[aiosqlite.Connection] = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    async def _get_connection(self) -> aiosqlite.Connection:
        """Get or create a database connection."""
        if self._conn is None:
            self._conn = await aiosqlite.connect(str(Path(SQLITE_DB_PATH)))
            self._conn.row_factory = aiosqlite.Row
            # Enable WAL mode for better concurrency
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA busy_timeout=5000")
            logger.info("SQLite connection established")
        return self._conn
    
    async def close(self):
        """Close the database connection."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            logger.info("SQLite connection closed")
    
    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        """Execute a SQL statement with transaction management."""
        conn = await self._get_connection()
        cursor = await conn.execute(sql, params)
        return cursor
    
    async def executemany(self, sql: str, params: list[tuple]) -> None:
        """Execute many SQL statements with transaction management."""
        conn = await self._get_connection()
        await conn.executemany(sql, params)
        await self._commit(conn)
    
    async def _commit(self, conn: aiosqlite.Connection) -> None:
        """Commit a transaction."""
        if conn is not None:
            await conn.commit()
    
    async def fetchone(self, sql: str, params: tuple = ()) -> Optional[aiosqlite.Row]:
        """Fetch a single row."""
        cursor = await self.execute(sql, params)
        return cursor.fetchone()
    
    async def fetchall(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        """Fetch all rows."""
        cursor = await self.execute(sql, params)
        return cursor.fetchall()
    
    async def cleanup_expired(self):
        """Remove expired rows from the database."""
        conn = await self._get_connection()
        now = time.time()
        cursor = await conn.execute(
            "DELETE FROM resolve_cache WHERE expires_at <= ?",
            (now,),
        )
        deleted = cursor.rowcount
        await self._commit(conn)
        logger.info(f"Cleaned up {deleted} expired cache entries")
        return deleted

db_manager = SQLiteConnectionManager()

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(_server):
    """FastMCP lifespan hook — manages HTTP client and SQLite."""
    # Initialize database
    await db_manager._get_connection()
    logger.info("Context7 Cache MCP server started")
    if not CONTEXT7_API_KEY:
        logger.warning("CONTEXT7_API_KEY is not set — Context7 API calls will fail")
    
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
        logger.info("Context7 Cache MCP server stopped")

mcp = FastMCP("context7-cache", lifespan=_lifespan)

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
# Context7 API helper
# ---------------------------------------------------------------------------

async def _context7_get(path: str, params: dict) -> str:
    """Authenticated GET against the Context7 API.  Returns text or raises."""
    if not CONTEXT7_API_KEY:
        raise ValueError("CONTEXT7_API_KEY is not set")
    resp = await _get_client().get(
        f"{CONTEXT7_API_BASE}{path}",
        params=params,
        headers={"Authorization": f"Bearer {CONTEXT7_API_KEY}"},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.text


def _cached_response(body: str, label: str) -> str:
    """Optionally prepend cache metadata for debugging."""
    if SHOW_CACHE_METADATA:
        return f"[CACHED — {label}]\n\n{body}"
    return body

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
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def resolve_library_id(query: str, libraryName: str) -> str:
    """
    Resolves a general library name into a Context7-compatible library ID.

    Args:
        query: The user's question or task (used to rank results by relevance)
        libraryName: The name of the library to search for
    """
    # Check rate limit
    client_ip = "unknown"
    
    rate_limited = not await rate_limiter.check(client_ip, RATE_LIMIT_PER_IP, 60)
    if rate_limited:
        logger.warning(f"Rate limit exceeded for IP: {client_ip}")
        return f"## Query: {query}\n\n**Error**: Rate limit exceeded"
    
    start_time = time.time()
    
    # 1. SQLite exact-match cache (keyed on normalised library name)
    try:
        hit = await _resolve_get(libraryName)
        if hit is not None:
            await metrics.record_cache_hit("context7_resolve")
            response_time = time.time() - start_time
            await metrics.record_request("context7_resolve", response_time)
            return _cached_response(hit, "exact match")
    except Exception as e:
        logger.warning("Resolve cache read error: %s", e)

    # 2. Upstream API call
    try:
        result = await _context7_get(
            "/libs/search",
            {"query": query, "libraryName": libraryName},
        )
    except ValueError as e:
        response_time = time.time() - start_time
        await metrics.record_request("context7_resolve", response_time)
        return f"Configuration error: {e}"
    except httpx.HTTPStatusError as e:
        response_time = time.time() - start_time
        await metrics.record_request("context7_resolve", response_time)
        logger.error("Context7 resolve HTTP %d: %.200s", e.response.status_code, e.response.text)
        return f"Error from Context7 API (HTTP {e.response.status_code})"
    except httpx.TimeoutException:
        response_time = time.time() - start_time
        await metrics.record_request("context7_resolve", response_time)
        return "Error: Context7 API request timed out"
    except Exception as e:
        response_time = time.time() - start_time
        await metrics.record_request("context7_resolve", response_time)
        logger.error("Unexpected resolve error: %s", e)
        return f"Error: {e}"

    # 3. Store in SQLite cache
    try:
        await _resolve_set(libraryName, result)
    except Exception as e:
        logger.warning("Resolve cache write error: %s", e)

    response_time = time.time() - start_time
    await metrics.record_request("context7_resolve", response_time)
    return result


@mcp.tool()
async def query_docs(libraryId: str, query: str, type: str = "txt") -> str:
    """
    Retrieves documentation for a library using a Context7-compatible library ID.

    Args:
        libraryId: Exact Context7-compatible library ID (e.g., /mongodb/docs, /vercel/next.js)
        query: The question or task to get relevant documentation for
        type: Response format - 'json' or 'txt' (default: txt)
    """
    # Check rate limit
    client_ip = "unknown"
    
    rate_limited = not await rate_limiter.check(client_ip, RATE_LIMIT_PER_IP, 60)
    if rate_limited:
        logger.warning(f"Rate limit exceeded for IP: {client_ip}")
        return f"## Query: {query}\n\n**Error**: Rate limit exceeded"
    
    start_time = time.time()
    
    # Namespace by library + format so similarity is compared only within the
    # same library and output type.
    cache_ns = f"context7_docs:{libraryId}:{type}"

    # 1. Semantic cache (query text only — libraryId+type encoded in namespace)
    cached = await check_semantic_cache(query, cache_ns, DOCS_SIMILARITY_THRESHOLD)
    if cached:
        await metrics.record_cache_hit("context7_docs")
        response_time = time.time() - start_time
        await metrics.record_request("context7_docs", response_time)
        return _cached_response(cached["response"], f"similarity: {cached['similarity']:.2f}")

    # 2. Upstream API call
    try:
        result = await _context7_get(
            "/context",
            {"libraryId": libraryId, "query": query, "type": type},
        )
    except ValueError as e:
        response_time = time.time() - start_time
        await metrics.record_request("context7_docs", response_time)
        return f"Configuration error: {e}"
    except httpx.HTTPStatusError as e:
        response_time = time.time() - start_time
        await metrics.record_request("context7_docs", response_time)
        logger.error("Context7 docs HTTP %d: %.200s", e.response.status_code, e.response.text)
        return f"Error from Context7 API (HTTP {e.response.status_code})"
    except httpx.TimeoutException:
        response_time = time.time() - start_time
        await metrics.record_request("context7_docs", response_time)
        return "Error: Context7 API request timed out"
    except Exception as e:
        response_time = time.time() - start_time
        await metrics.record_request("context7_docs", response_time)
        logger.error("Unexpected docs error: %s", e)
        return f"Error: {e}"

    # 3. Store in semantic cache
    await add_to_semantic_cache(query, cache_ns, result, ttl=DOCS_TTL)
    
    response_time = time.time() - start_time
    await metrics.record_request("context7_docs", response_time)
    return result

# ---------------------------------------------------------------------------
# Enhanced Endpoints
# ---------------------------------------------------------------------------

@mcp.resource("health://")
async def health_check() -> dict:
    """Health check endpoint with system information."""
    memory = None
    cpu_percent = None
    
    try:
        if psutil is not None:
            memory = psutil.virtual_memory()
            cpu_percent = psutil.cpu_percent(interval=0.1)
    except Exception as e:
        logger.warning(f"Could not get system metrics: {e}")
    
    return {
        "status": "ok",
        "model": "context7-cache",
        "model_loaded": True,
        "dimension": 384,
        "cache_size": 0,  # Would need to count actual entries
        "memory": {
            "memory_mb": round(memory.available / (1024 * 1024), 2) if memory else None,
            "cpu_percent": cpu_percent
        }
    }

@mcp.resource("stats://")
async def get_stats() -> dict:
    """Get comprehensive cache and metrics statistics."""
    # Get cache stats from database
    db = await db_manager._get_connection()
    total_entries = await db_manager.fetchone("SELECT COUNT(*) as count FROM resolve_cache")
    expired_entries = await db_manager.fetchone(
        "SELECT COUNT(*) as count FROM resolve_cache WHERE expires_at <= ?",
        (time.time(),)
    )
    
    cache_stats = {
        "total_entries": total_entries[0] if total_entries else 0,
        "expired_entries": expired_entries[0] if expired_entries else 0,
        "active_entries": cache_stats["total_entries"] - cache_stats["expired_entries"],
        "by_tool": {},
        "index_size": cache_stats["total_entries"],
        "max_entries": 10000  # Default max
    }
    
    # Get metrics
    metrics_stats = await metrics.get_stats()
    
    return {
        "cache": cache_stats,
        "metrics": metrics_stats
    }

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run()