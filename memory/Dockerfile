# mcp-memory-service pinned from PyPI (replaces the deleted upstream git repo
# github.com/doobidoo/mcp-memory-service which returned 404 on 2026-08-04).
# Bump MEMORY_VERSION and rebuild to upgrade.
FROM python:3.12-slim

ARG MEMORY_VERSION=11.6.1

# System deps for sqlite-vec + sentence-transformers wheel builds.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates curl gcc libffi-dev && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Pin the exact version. The [sqlite-ml] extra pulls onnxruntime,
# sentence-transformers, tokenizers, torch — required for the sqlite_vec
# backend to compute embeddings compatible with the existing memories.db.
RUN pip install --no-cache-dir "mcp-memory-service[sqlite-ml]==${MEMORY_VERSION}"

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8765

# Run the streamable-HTTP MCP server. Host/port/backend/db path are read from
# env vars set in docker-compose.yml (MCP_SSE_HOST, MCP_SSE_PORT,
# MCP_MEMORY_STORAGE_BACKEND, MCP_MEMORY_SQLITE_PATH, MCP_ALLOW_ANONYMOUS_ACCESS).
CMD ["memory", "server", "--streamable-http"]
