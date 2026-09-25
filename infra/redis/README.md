# Redis — Vision Gateway Cache

```bash
docker compose --profile infra up -d redis
# or
docker run -d --name nimna-redis -p 6379:6379 -v $(pwd)/infra/redis/redis.conf:/usr/local/etc/redis/redis.conf redis:7-alpine redis-server /usr/local/etc/redis/redis.conf
```

- `REDIS_URL=redis://localhost:6379/0` (or `redis://redis:6379/0` inside Docker/K8s)
- `VISION_CACHE_TTL=600` (seconds)
- Fallback: in-memory LRU (256 entries) if Redis unavailable — no break.

Check hit rate:
```bash
curl -s http://localhost:8001/api/health | jq .cache
redis-cli info memory
```
