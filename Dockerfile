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
RUN pip install --upgrade pip && pip install .

COPY skills ./skills
COPY workspace ./workspace
RUN mkdir -p data workspace/reports && chown -R nimna:nimna /app

USER nimna
EXPOSE 8000

ENV HOST=0.0.0.0 PORT=8000 SKILLS_DIR=/app/skills WORKSPACE_DIR=/app/workspace DB_PATH=/app/data/nimna.db

CMD ["nimna", "serve"]
