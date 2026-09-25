# Nimna agent – API + web UI
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# non-root user; the workspace and data dirs are volumes
RUN useradd --create-home --uid 1000 nimna

COPY pyproject.toml README.md ./
COPY nimna ./nimna
# ".[infra]": redis/qdrant/numpy/prometheus-client (كاش الرؤية، الذاكرة الشعاعية، /metrics)
# كانت تضيع سابقًا لأنها خارج تبعيات pyproject — الانظر P3-4 في docs/REPO-AUDIT-2026-09-25.md
RUN pip install --upgrade "pip>=26.2" "setuptools>=83" && pip install ".[infra]"

COPY skills ./skills
COPY workspace ./workspace
RUN mkdir -p data workspace/reports && chown -R nimna:nimna /app

USER nimna
EXPOSE 8000

ENV HOST=0.0.0.0 PORT=8000 SKILLS_DIR=/app/skills WORKSPACE_DIR=/app/workspace DB_PATH=/app/data/nimna.db

CMD ["nimna", "serve"]
