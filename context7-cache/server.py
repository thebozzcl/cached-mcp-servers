#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mcp>=1.0.0",
#   "httpx>=0.27",
#   "anyio>=4.5",
#   "aiosqlite>=0.20",
# ]
# ///
"""
Context7 Cache MCP Server — Wraps Context7 with local caching.

Caching strategy
----------------
  resolve_library_id : SQLite exact-match cache (keyed on normalised library name)
  query_docs         : semantic similarity cache   (scoped per library + output format)
"""

import os
import time
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
import httpx
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

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("context7-cache")

# ---------------------------------------------------------------------------
# Shared HTTP client (lifespan-managed, with lazy-init fallback)
# ---------------------------------------------------------------------------
_http_client: httpx.AsyncClient | None = None


def _build_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=5),
    )


def _get_client() -> httpx.AsyncClient:
    """Return the shared HTTP client, creating it lazily if needed."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = _build_client()
    return _http_client


# ---------------------------------------------------------------------------
# SQLite exact-match cache for library resolution
# ---------------------------------------------------------------------------
_db: aiosqlite.Connection | None = None

_INIT_SQL = """
CREATE TABLE IF NOT EXISTS resolve_cache (
    library_key  TEXT PRIMARY KEY,
    response     TEXT NOT NULL,
    expires_at   REAL NOT NULL
);
"""

_PRUNE_SQL = "DELETE FROM resolve_cache WHERE expires_at <= ?;"


def _normalize_lib_name(name: str) -> str:
    """Lowercase, strip whitespace / common separators for matching."""
    return name.strip().lower().replace("-", "").replace("_", "").replace(" ", "")


async def _init_db() -> aiosqlite.Connection:
    """Open (or create) the SQLite database and ensure the schema exists."""
    db_path = Path(SQLITE_DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(db_path))
    db.row_factory = aiosqlite.Row
    await db.execute(_INIT_SQL)
    # Prune expired rows on startup
    await db.execute(_PRUNE_SQL, (time.time(),))
    await db.commit()
    logger.info("SQLite resolve cache opened at %s", db_path)
    return db


async def _get_db() -> aiosqlite.Connection:
    """Return the shared DB connection, initialising lazily if needed."""
    global _db
    if _db is None:
        _db = await _init_db()
    return _db


async def _resolve_get(library_name: str) -> str | None:
    """Look up a cached resolve result. Returns response text or None."""
    db = await _get_db()
    key = _normalize_lib_name(library_name)
    now = time.time()
    async with db.execute(
        "SELECT response FROM resolve_cache WHERE library_key = ? AND expires_at > ?",
        (key, now),
    ) as cursor:
        row = await cursor.fetchone()
    if row is not None:
        return row[0]
    return None


async def _resolve_set(library_name: str, response: str) -> None:
    """Insert or update a cached resolve result."""
    db = await _get_db()
    key = _normalize_lib_name(library_name)
    expires_at = time.time() + RESOLVE_TTL
    await db.execute(
        """
        INSERT INTO resolve_cache (library_key, response, expires_at)
        VALUES (?, ?, ?)
        ON CONFLICT(library_key) DO UPDATE SET response = excluded.response,
                                                expires_at = excluded.expires_at
        """,
        (key, response, expires_at),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def _lifespan(_server):
    """FastMCP lifespan hook — manages shared HTTP client and SQLite."""
    global _http_client, _db
    _http_client = _build_client()
    _db = await _init_db()
    logger.info("Context7 Cache MCP server started")
    if not CONTEXT7_API_KEY:
        logger.warning("CONTEXT7_API_KEY is not set — Context7 API calls will fail")
    try:
        yield
    finally:
        if _db is not None:
            await _db.close()
            _db = None
        if _http_client and not _http_client.is_closed:
            await _http_client.aclose()
        logger.info("Context7 Cache MCP server stopped")


mcp = FastMCP("context7-cache", lifespan=_lifespan)

# ---------------------------------------------------------------------------
# Semantic cache helpers
# ---------------------------------------------------------------------------
async def _semantic_search(query: str, tool: str, threshold: float) -> dict | None:
    """Return cache entry dict (keys: response, similarity) on hit, else None."""
    try:
        resp = await _get_client().post(
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
    except httpx.TimeoutException:
        logger.warning("Semantic cache search timed out")
    except Exception as e:
        logger.warning("Semantic cache search error: %s", e)
    return None


async def _semantic_store(query: str, tool: str, response: str, ttl: int) -> None:
    """Add a result to the semantic cache (fire-and-forget on failure)."""
    try:
        resp = await _get_client().post(
            f"{SEMANTIC_CACHE_URL}/add",
            json={"query": query, "tool": tool, "response": response, "ttl": ttl},
            timeout=5.0,
        )
        resp.raise_for_status()
    except httpx.TimeoutException:
        logger.warning("Semantic cache store timed out")
    except Exception as e:
        logger.warning("Semantic cache store error: %s", e)


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
    # 1. SQLite exact-match cache (keyed on normalised library name)
    try:
        hit = await _resolve_get(libraryName)
        if hit is not None:
            logger.info("Resolve exact-cache HIT for '%s'", libraryName)
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
        return f"Configuration error: {e}"
    except httpx.HTTPStatusError as e:
        logger.error("Context7 resolve HTTP %d: %.200s", e.response.status_code, e.response.text)
        return f"Error from Context7 API (HTTP {e.response.status_code})"
    except httpx.TimeoutException:
        return "Error: Context7 API request timed out"
    except Exception as e:
        logger.error("Unexpected resolve error: %s", e)
        return f"Error: {e}"

    # 3. Store in SQLite cache
    try:
        await _resolve_set(libraryName, result)
    except Exception as e:
        logger.warning("Resolve cache write error: %s", e)

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
    # Namespace by library + format so similarity is compared only within the
    # same library and output type.
    cache_ns = f"context7_docs:{libraryId}:{type}"

    # 1. Semantic cache (query text only — libraryId+type encoded in namespace)
    cached = await _semantic_search(query, cache_ns, DOCS_SIMILARITY_THRESHOLD)
    if cached:
        return _cached_response(cached["response"], f"similarity: {cached['similarity']:.2f}")

    # 2. Upstream API call
    try:
        result = await _context7_get(
            "/context",
            {"libraryId": libraryId, "query": query, "type": type},
        )
    except ValueError as e:
        return f"Configuration error: {e}"
    except httpx.HTTPStatusError as e:
        logger.error("Context7 docs HTTP %d: %.200s", e.response.status_code, e.response.text)
        return f"Error from Context7 API (HTTP {e.response.status_code})"
    except httpx.TimeoutException:
        return "Error: Context7 API request timed out"
    except Exception as e:
        logger.error("Unexpected docs error: %s", e)
        return f"Error: {e}"

    # 3. Store in semantic cache
    await _semantic_store(query, cache_ns, result, ttl=DOCS_TTL)
    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run()

