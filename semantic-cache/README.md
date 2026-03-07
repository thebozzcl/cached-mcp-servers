# Semantic Cache Service

A standalone systemd service for semantic caching with FAISS and sentence-transformers.

## Features

- Semantic caching for API responses
- In-memory vector search using FAISS
- Automatic TTL-based cleanup
- Configurable similarity thresholds
- Health checks and metrics
- Automatic restart on failure

## Installation

### Automatic Setup (User-Level Service)

Run the setup script:

```bash
cd semantic-cache
./setup.sh
```

This will:
- Create the cache directory
- Install the service file in `~/.config/systemd/user/`
- Enable and start the service

### Manual Setup

If you prefer manual setup:

1. **Copy the service file:**
   ```bash
   cp semantic-cache.service ~/.config/systemd/user/
   ```

2. **Create cache directory:**
   ```bash
   mkdir -p /home/bozz/.cache/semantic-cache/cache
   ```

3. **Reload systemd daemon:**
   ```bash
   systemctl --user daemon-reload
   ```

4. **Enable and start the service:**
   ```bash
   systemctl --user enable semantic-cache.service
   systemctl --user start semantic-cache.service
   ```

## Environment Variables

You can configure the service through environment variables in the service file or by setting them before starting:

- `CACHE_DIR`: Cache storage directory (default: `/home/bozz/.cache/semantic-cache`)
- `EMBEDDING_MODEL`: Sentence transformer model name (default: `all-MiniLM-L6-v2`)
- `SIMILARITY_THRESHOLD`: Minimum similarity for cache hits (default: `0.92`)
- `MAX_ENTRIES`: Maximum cache entries before eviction (default: `10000`)
- `MAX_RESPONSE_LENGTH`: Maximum response length in bytes (default: `500000`)
- `CLEANUP_INTERVAL`: Seconds between cleanup runs (default: `3600`)
- `SAVE_EVERY_N_ADDS`: Save to disk every N additions (default: `10`)
- `CACHE_API_KEY`: Optional API key for authentication (default: empty)

## Usage

### Service Management

```bash
# Check status
systemctl --user status semantic-cache

# Start service
systemctl --user start semantic-cache

# Stop service
systemctl --user stop semantic-cache

# Restart service
systemctl --user restart semantic-cache

# View logs
journalctl --user -u semantic-cache -f

# View recent logs
journalctl --user -u semantic-cache -n 50

# Disable service (won't start on boot)
systemctl --user disable semantic-cache
```

### API Endpoints

The service runs on `http://127.0.0.1:7437`

- **Health Check:**
  ```bash
  curl http://127.0.0.1:7437/health
  ```

- **Search cached responses:**
  ```bash
  curl -X POST http://127.0.0.1:7437/search \
    -H "Content-Type: application/json" \
    -d '{"query":"your query here","tool":"kagi_search","threshold":0.92}'
  ```

- **Add response to cache:**
  ```bash
  curl -X POST http://127.0.0.1:7437/add \
    -H "Content-Type: application/json" \
    -d '{"query":"your query here","tool":"kagi_search","response":"cached response"}'
  ```

- **Clear cache:**
  ```bash
  curl -X POST http://127.0.0.1:7437/clear \
    -H "Content-Type: application/json" \
    -d '{"tool":"kagi_search"}'
  ```

- **Get stats:**
  ```bash
  curl http://127.0.0.1:7437/stats
  ```

## Security

The service includes several security measures:

- **NoNewPrivileges**: Prevents privilege escalation
- **PrivateTmp**: Isolates temporary files
- **ProtectSystem**: Limits access to system files
- **ProtectHome**: Limits access to home directory
- **ReadWritePaths**: Only allows writes to cache directory

## Troubleshooting

### Service won't start

1. Check logs: `journalctl -u semantic-cache -n 50`
2. Verify uv is installed: `which uv`
3. Check if port 7437 is available: `netstat -tlnp | grep 7437`
4. Verify Python dependencies: `uv run --help`

### High memory usage

The sentence-transformer model loads into memory. If you notice high memory usage:

1. Consider using a smaller model (e.g., `all-MiniLM-L6-v2` vs `all-mpnet-base-v2`)
2. Reduce `MAX_ENTRIES`
3. Adjust TTLs for different tools

### Cache not persisting

1. Verify cache directory permissions: `ls -la /home/bozz/.cache/semantic-cache`
2. Check disk space: `df -h`
3. Review logs for save errors: `journalctl -u semantic-cache | grep -i save`

## Dependencies

The service requires:

- Python 3.8+
- uv (package manager)
- FastAPI, Uvicorn, sentence-transformers, faiss-cpu, numpy, pydantic, psutil

Dependencies are installed automatically via uv when the service starts.

## Integration with MCP Servers

This service can be used by other MCP servers (kagimcp-cache, context7-cache) as a shared semantic cache backend.

## License

See main project license.