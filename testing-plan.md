# Testing Plan for Cached MCP Servers

## Overview
This document outlines a comprehensive testing strategy for the three MCP servers:
- `semantic-cache` (core caching service)
- `kagimcp-cache` (Kagi API wrapper with caching)
- `context7-cache` (Context7 API wrapper with caching)

## Current State Analysis

### Semantic Cache Server
**Strengths:**
- Modular design with clear separation of concerns
- Well-structured classes (SemanticCache, Metrics)
- Environment-based configuration
- Comprehensive logging

**Testability Issues:**
- Global state management (model, cache, cleanup timer)
- Tight coupling to external dependencies (FAISS, sentence-transformers)
- No dependency injection
- Hard-coded paths and configurations
- Background cleanup task not easily testable

### Kagi Cache Server
**Strengths:**
- Singleton pattern for database connection
- Async/await throughout
- Clear separation of concerns
- Retry logic for resilience

**Testability Issues:**
- Global HTTP client
- Singleton pattern makes mocking difficult
- Tight coupling to external HTTP APIs
- No dependency injection
- Background cleanup task not easily testable

### Context7 Cache Server
**Strengths:**
- Similar structure to kagimcp-cache
- Good separation of concerns
- Async/await throughout

**Testability Issues:**
- Same issues as kagimcp-cache
- Global HTTP client
- Singleton pattern for database
- Tight coupling to external APIs

---

## Test Strategy

### 1. Unit Tests (Core Logic)

#### semantic-cache
**Test Cases:**
- [ ] Model loading and dimension retrieval
- [ ] Cache initialization and index creation
- [ ] Search with threshold filtering
- [ ] Add entry with deduplication
- [ ] TTL expiration logic
- [ ] Tool family matching
- [ ] Eviction logic (capacity management)
- [ ] Persistence (load/save to disk)
- [ ] Metrics recording

**Mocking Strategy:**
- Mock FAISS Index operations
- Mock sentence-transformers encoding
- Mock file I/O for persistence
- Mock time for TTL tests

#### kagimcp-cache
**Test Cases:**
- [ ] SQLite connection management
- [ ] Summary cache (exact match) logic
- [ ] Semantic cache check with retry logic
- [ ] Rate limiting logic
- [ ] Metrics recording
- [ ] Database cleanup logic

**Mocking Strategy:**
- Mock SQLite operations
- Mock HTTP client responses
- Mock time for TTL tests

#### context7-cache
**Test Cases:**
- [ ] SQLite connection management
- [ ] Resolve cache (exact match) logic
- [ ] Semantic cache check with retry logic
- [ ] Rate limiting logic
- [ ] Metrics recording
- [ ] Database cleanup logic

**Mocking Strategy:**
- Mock SQLite operations
- Mock HTTP client responses
- Mock time for TTL tests

### 2. Integration Tests (API Endpoints)

#### semantic-cache
**Test Cases:**
- [ ] POST /search - successful search
- [ ] POST /search - cache hit
- [ ] POST /search - cache miss
- [ ] POST /add - successful add
- [ ] POST /add - duplicate detection
- [ ] POST /clear - tool-specific clear
- [ ] POST /clear - all clear
- [ ] GET /health - successful response
- [ ] GET /stats - successful response
- [ ] POST /add with invalid data - error handling
- [ ] POST /search with invalid data - error handling

**Mocking Strategy:**
- Mock HTTP requests to external services
- Mock FAISS index behavior
- Mock file system for persistence

#### kagimcp-cache
**Test Cases:**
- [ ] search tool - successful search
- [ ] search tool - cache hit
- [ ] search tool - cache miss
- [ ] search tool - rate limiting
- [ ] search tool - API error handling
- [ ] fastgpt tool - successful call
- [ ] fastgpt tool - cache hit
- [ ] summarize tool - successful call
- [ ] summarize tool - cache hit

**Mocking Strategy:**
- Mock HTTP client responses
- Mock semantic-cache server
- Mock SQLite database

#### context7-cache
**Test Cases:**
- [ ] resolve_library_id tool - successful call
- [ ] resolve_library_id tool - cache hit
- [ ] resolve_library_id tool - rate limiting
- [ ] query_docs tool - successful call
- [ ] query_docs tool - cache hit
- [ ] query_docs tool - rate limiting

**Mocking Strategy:**
- Mock HTTP client responses
- Mock semantic-cache server
- Mock SQLite database

### 3. End-to-End Tests

**Test Cases:**
- [ ] Full workflow: search → cache → search again (hit)
- [ ] Full workflow: query_docs → cache → query_docs again (hit)
- [ ] Full workflow: summarize → cache → summarize again (hit)
- [ ] Cache expiration workflow
- [ ] Cleanup task execution

**Mocking Strategy:**
- Mock all external dependencies
- Use in-memory databases
- Use mock HTTP servers

### 4. Performance Tests

**Test Cases:**
- [ ] Search performance with large dataset
- [ ] Add performance with many entries
- [ ] Cache hit rate with realistic data
- [ ] Memory usage with large cache

---

## Refactoring for Testability

### 1. Dependency Injection

**Current:**
```python
# Global state
model: Optional[SentenceTransformer] = None
cache: Optional["SemanticCache"] = None
```

**Recommended:**
```python
class SemanticCache:
    def __init__(self, model: SentenceTransformer, cache_dir: Path):
        self.model = model
        self.cache_dir = cache_dir
        # ... rest of initialization
```

### 2. Remove Global State

**Current:**
```python
def load_model():
    global model, DIMENSION
    # ...
```

**Recommended:**
```python
def load_model(model_name: str) -> SentenceTransformer:
    return SentenceTransformer(model_name)
```

### 3. Make Cleanup Task Testable

**Current:**
```python
def _schedule_cleanup(cache_inst: SemanticCache, interval: int):
    # Global timer
```

**Recommended:**
```python
class CleanupScheduler:
    def __init__(self, cleanup_func, interval):
        self.cleanup_func = cleanup_func
        self.interval = interval
        self._task = None
    
    async def start(self):
        self._task = asyncio.create_task(self._run())
    
    async def stop(self):
        if self._task:
            self._task.cancel()
```

### 4. HTTP Client Management

**Current:**
```python
_http_client: Optional[httpx.AsyncClient] = None

async def _get_client() -> httpx.AsyncClient:
    global _http_client
    # ...
```

**Recommended:**
```python
class HTTPClientManager:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self._client: Optional[httpx.AsyncClient] = None
    
    async def get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=5)
            )
        return self._client
    
    async def close(self):
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
```

### 5. Database Manager Refactoring

**Current:**
```python
class SQLiteConnectionManager:
    _instance: Optional['SQLiteConnectionManager'] = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
```

**Recommended:**
```python
class SQLiteConnectionManager:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
    
    async def get_connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
        return self._conn
    
    async def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None
```

### 6. Configuration Management

**Current:**
```python
CACHE_DIR = Path(os.environ.get("CACHE_DIR", ...))
MODEL_NAME = os.environ.get("EMBEDDING_MODEL", ...)
```

**Recommended:**
```python
@dataclass
class Config:
    cache_dir: Path
    model_name: str
    default_threshold: float
    max_entries: int
    # ... other config
    
    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            cache_dir=Path(os.environ.get("CACHE_DIR", ...)),
            model_name=os.environ.get("EMBEDDING_MODEL", ...),
            # ...
        )
```

---

## Test Infrastructure

### 1. Test Dependencies

**semantic-cache:**
```txt
pytest>=7.0.0
pytest-asyncio>=0.21.0
pytest-mock>=3.10.0
httpx>=0.27.0
faiss-cpu>=1.7.0
sentence-transformers>=2.2.0
```

**kagimcp-cache & context7-cache:**
```txt
pytest>=7.0.0
pytest-asyncio>=0.21.0
pytest-mock>=3.10.0
httpx>=0.27.0
aiosqlite>=0.20.0
```

### 2. Test Structure

```
semantic-cache/
├── tests/
│   ├── __init__.py
│   ├── conftest.py          # Shared fixtures
│   ├── test_cache.py        # SemanticCache tests
│   ├── test_metrics.py      # Metrics tests
│   ├── test_api.py          # API endpoint tests
│   └── test_persistence.py  # Persistence tests

kagimcp-cache/
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── test_database.py     # SQLite tests
│   ├── test_semantic_cache.py
│   ├── test_api.py
│   └── test_rate_limiter.py

context7-cache/
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── test_database.py
│   ├── test_semantic_cache.py
│   └── test_api.py
```

### 3. Key Fixtures

```python
# conftest.py
import pytest
from pathlib import Path
import tempfile
import shutil

@pytest.fixture
def temp_cache_dir():
    """Create a temporary directory for cache storage."""
    tmpdir = tempfile.mkdtemp()
    yield Path(tmpdir)
    shutil.rmtree(tmpdir)

@pytest.fixture
def mock_model():
    """Mock sentence-transformers model."""
    with patch('sentence_transformers.SentenceTransformer') as mock:
        mock_instance = MagicMock()
        mock_instance.encode.return_value = np.array([0.1, 0.2, 0.3])
        mock_instance.get_sentence_embedding_dimension.return_value = 3
        mock.return_value = mock_instance
        return mock_instance

@pytest.fixture
def mock_faiss_index():
    """Mock FAISS index."""
    with patch('faiss.IndexFlatIP') as mock:
        mock_instance = MagicMock()
        mock_instance.ntotal = 0
        mock_instance.search.return_value = (np.array([[0.9, 0.8, 0.7]]), np.array([[0, 1, 2]]))
        mock.return_value = mock_instance
        return mock_instance

@pytest.fixture
def mock_http_client():
    """Mock HTTP client."""
    with patch('httpx.AsyncClient') as mock:
        mock_instance = MagicMock()
        mock_instance.post.return_value.json.return_value = {"hit": True, "similarity": 0.95}
        mock.return_value = mock_instance
        return mock_instance
```

---

## Testing Priorities

### Phase 1: Critical Path (High Priority)
1. [ ] semantic-cache: Core cache operations (search, add, eviction)
2. [ ] semantic-cache: TTL expiration logic
3. [ ] kagimcp-cache: SQLite cache operations
4. [ ] kagimcp-cache: Semantic cache integration
5. [ ] context7-cache: SQLite cache operations
6. [ ] context7-cache: Semantic cache integration

### Phase 2: API Endpoints (Medium Priority)
1. [ ] All API endpoint tests
2. [ ] Error handling tests
3. [ ] Rate limiting tests

### Phase 3: Advanced Features (Low Priority)
1. [ ] Performance tests
2. [ ] Load tests
3. [ ] Memory leak tests
4. [ ] Concurrency tests

---

## Risks and Mitigations

### Risk 1: External Dependencies
**Risk:** Tests depend on external services (FAISS, sentence-transformers, HTTP APIs)
**Mitigation:** Use mocking extensively, create test fixtures for common scenarios

### Risk 2: Global State
**Risk:** Global state makes tests flaky and hard to run in parallel
**Mitigation:** Refactor to use dependency injection, remove global state

### Risk 3: File System Dependencies
**Risk:** Tests depend on file system for persistence
**Mitigation:** Use in-memory databases, temporary directories, mock file I/O

### Risk 4: Timing Issues
**Risk:** TTL and cleanup tasks depend on time
**Mitigation:** Use time mocking, parameterize TTL values

### Risk 5: Database State
**Risk:** Database state persists between tests
**Mitigation:** Use test databases, cleanup after each test, use fixtures

---

## Implementation Plan

### Step 1: Set Up Testing Infrastructure
- [ ] Create test directories
- [ ] Add test dependencies
- [ ] Create conftest.py with shared fixtures
- [ ] Set up pytest configuration

### Step 2: Refactor for Testability
- [ ] Remove global state from semantic-cache
- [ ] Add dependency injection to semantic-cache
- [ ] Remove global state from kagimcp-cache
- [ ] Add dependency injection to kagimcp-cache
- [ ] Remove global state from context7-cache
- [ ] Add dependency injection to context7-cache

### Step 3: Write Unit Tests
- [ ] semantic-cache: Core logic tests
- [ ] kagimcp-cache: Core logic tests
- [ ] context7-cache: Core logic tests

### Step 4: Write Integration Tests
- [ ] semantic-cache: API endpoint tests
- [ ] kagimcp-cache: API endpoint tests
- [ ] context7-cache: API endpoint tests

### Step 5: Write E2E Tests
- [ ] Full workflow tests
- [ ] Cache expiration tests
- [ ] Cleanup task tests

### Step 6: Performance Tests
- [ ] Performance benchmarks
- [ ] Load tests

---

## Success Criteria

- [ ] 80% code coverage for core logic
- [ ] All critical paths tested
- [ ] No flaky tests
- [ ] Tests run in parallel without issues
- [ ] CI/CD integration ready
- [ ] Documentation for running tests