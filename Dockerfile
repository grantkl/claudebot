FROM python:3.12-slim

# Install Node.js 22 (needed because Agent SDK spawns Claude CLI as subprocess)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg gcc libffi-dev && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI globally
RUN npm install -g @anthropic-ai/claude-code
RUN npm install -g @privilegemendes/amadeus-mcp-server && \
    sed -i 's/clientSecret: process.env.AMADEUS_CLIENT_SECRET,/clientSecret: process.env.AMADEUS_CLIENT_SECRET, hostname: process.env.AMADEUS_HOSTNAME || "test",/' \
    /usr/lib/node_modules/@privilegemendes/amadeus-mcp-server/dist/index.js
RUN npm install -g @modelcontextprotocol/server-brave-search

# Install Playwright MCP globally and browser dependencies for headless Chromium
RUN npm install -g @playwright/mcp && \
    npx --yes playwright install --with-deps chromium

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Install Python dependencies
COPY pyproject.toml .
RUN uv pip install --system --no-cache .

# Install Google Flights MCP server (HaroldLeo — fast-flights scraper with SerpAPI fallback)
RUN apt-get update && apt-get install -y --no-install-recommends git && \
    uv pip install --system --no-cache "mcp-server-google-flights @ git+https://github.com/HaroldLeo/google-flights-mcp.git" && \
    apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# Install GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && apt-get install -y --no-install-recommends gh && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Fix: google-flights-mcp passes seat_type with underscores (e.g. premium_economy)
# but fast_flights expects dashes (premium-economy), causing KeyError
RUN python3 -c "\
import pathlib; \
p = pathlib.Path('/usr/local/lib/python3.12/site-packages/mcp_server_google_flights/server.py'); \
p.write_text(p.read_text().replace('seat=seat_type,', 'seat=seat_type.replace(\"_\", \"-\"),'))"

# Copy application source and ensure readable permissions
COPY src/ src/
RUN chmod -R a+rX src/

# Create non-root user and switch to it
RUN useradd --create-home appuser

# Create directories for scheduler config and state
RUN mkdir -p /app/config /app/data && chown -R appuser:appuser /app/config /app/data

USER appuser

ENV PYTHONUNBUFFERED=1
ENV ENABLE_MCP=true

CMD ["python", "-m", "src.main"]
