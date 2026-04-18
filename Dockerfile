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

# Install Playwright CLI and browser dependencies for headless Chromium.
# Using a shared /opt path so both the Node CLI (used via Bash) and the
# Python `playwright` package (used by the fb_marketplace MCP) can find
# browsers when running as the non-root appuser.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN npm install -g @playwright/cli@latest && \
    mkdir -p "$PLAYWRIGHT_BROWSERS_PATH" && \
    npx --yes playwright install --with-deps chromium && \
    playwright-cli install-browser chromium && \
    chmod -R a+rX "$PLAYWRIGHT_BROWSERS_PATH"

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Install Python dependencies
COPY pyproject.toml .
RUN uv pip install --system --no-cache .

# Ensure Python playwright has a matching Chromium build in the shared browser
# path. This is a no-op if the npx install above already put the same version
# there; otherwise it adds a second build alongside it. System libs are already
# installed by the npx step above, so no --with-deps needed here.
RUN python -m playwright install chromium && \
    chmod -R a+rX "$PLAYWRIGHT_BROWSERS_PATH"

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

# Configure Playwright CLI: default to bundled Chromium (not Google Chrome which
# requires a separate install at /opt/google/chrome) and write output to a
# directory the non-root user can write to (CWD /app is read-only).
RUN mkdir -p /home/appuser/.playwright /home/appuser/.playwright-cli && \
    printf '{\n  "browser": {\n    "browserName": "chromium",\n    "launchOptions": {\n      "channel": "chromium",\n      "headless": true\n    }\n  },\n  "outputDir": "/home/appuser/.playwright-cli"\n}\n' \
      > /home/appuser/.playwright/cli.config.json && \
    chown -R appuser:appuser /home/appuser/.playwright /home/appuser/.playwright-cli

USER appuser

ENV PYTHONUNBUFFERED=1
ENV ENABLE_MCP=true

CMD ["python", "-m", "src.main"]
