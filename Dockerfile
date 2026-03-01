FROM python:3.12-slim

# Install Node.js 22 (needed because Agent SDK spawns Claude CLI as subprocess)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI globally
RUN npm install -g @anthropic-ai/claude-code

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Install Python dependencies
COPY pyproject.toml .
RUN uv pip install --system --no-cache .

# Copy application source
COPY src/ src/

# Create non-root user and switch to it
RUN useradd --create-home appuser

# Create directories for scheduler config and state
RUN mkdir -p /app/config /app/data && chown -R appuser:appuser /app/config /app/data

USER appuser

ENV PYTHONUNBUFFERED=1
ENV ENABLE_MCP=true

CMD ["python", "-m", "src.main"]
