# Valkey — Vision Gateway Cache (+ shared rate-limit buckets)

> The service/volume/env names keep the `redis` prefix for compatibility,
> but the server is **Valkey 8** (BSD-3-Clause, Linux Foundation) — a fork
> of Redis 7.2.4. Redis ≥ 7.4 moved to SSPL/RSAL (not OSI open source), so
> the floating `redis:7-alpine` tag was a license risk. Valkey speaks the
> same RESP protocol (Lua included), so **no application code changed**:
> the `redis://` URL scheme and the MIT-licensed `redis` Python client
> work unchanged. See `docs/FREE-STACK.md` for the verification record.

```bash
docker compose --profile infra up -d redis
# or
docker run -d --name nimna-redis -p 6379:6379 -v $(pwd)/infra/redis/redis.conf:/usr/local/etc/redis/redis.conf valkey/valkey:8 valkey-server /usr/local/etc/redis/redis.conf
```

- `REDIS_URL=redis://localhost:6379/0` (or `redis://redis:6379/0` inside Docker/K8s)
- `VISION_CACHE_TTL=600` (seconds)
- Fallback: in-memory LRU (256 entries) if Valkey unavailable — no break.

Check hit rate:
```bash
curl -s http://localhost:8001/api/health | jq .cache
valkey-cli info memory
```
