# Qdrant — Vector Memory (Sprint 2)

```bash
docker compose --profile infra up -d qdrant
# or
docker run -d -p 6333:6333 -p 6334:6334 -v qdrant_data:/qdrant/storage qdrant/qdrant:v1.11.0
```

- `QDRANT_URL=http://localhost:6333` (or `http://qdrant:6333` inside Docker/K8s)
- `QDRANT_API_KEY=` (optional, for cloud)
- `EMBEDDING_MODEL=text-embedding-004`, `EMBEDDING_DIM=768`, `EMBEDDING_PROVIDER=auto|gemini|hash`
- Fallback: deterministic hash embeddings (768-dim, L2 normalized) + in-memory cosine store — **no Qdrant required for tests/offline**.

Collections (auto-created):
- `user_context` — تفضيلات/أنماط
- `execution_history` — سجلات Shell (Self-Healing)
- `code_knowledge` — snippets

Check:
```bash
curl -s http://localhost:6333/healthz
curl -s http://localhost:8000/api/health | jq .memory
curl -s http://localhost:8000/api/memory/vector/health | jq
```
