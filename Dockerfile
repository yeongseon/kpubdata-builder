# Builder service image - runs the HTTP API server
#
# This image uses --no-sources to ignore [tool.uv.sources] editable overrides
# and installs kpubdata from PyPI using the version pin in pyproject.toml.
# See ADR 0006 (#312) for deployment and authentication decisions.

FROM python:3.13-slim

LABEL maintainer="yeongseon <yeongseon@gmail.com>"
LABEL description="KPubData Builder HTTP service - dataset build pipeline API"

# Install uv for fast dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Set working directory
WORKDIR /app

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies from PyPI (no editable sources)
RUN uv sync --no-sources --frozen

# Copy application source
COPY src/ /app/src/

# Create output directory for build artifacts
RUN mkdir -p /data/builds

# Environment variables
# KPUBDATA_BUILDER_API_KEY: Required for authentication (fail-closed)
# KPUBDATA_BUILDER_PORT: HTTP port (default: 8000)
# KPUBDATA_BUILDER_OUTPUT_ROOT: Output directory for build artifacts (default: ./data/builds)
# KPUBDATA_BUILDER_ALLOWED_ORIGINS: Comma-separated list of allowed CORS origins (optional)

ENV KPUBDATA_BUILDER_PORT=8000
ENV KPUBDATA_BUILDER_OUTPUT_ROOT=/data/builds

# Expose HTTP port
EXPOSE 8000

# Fail-closed: API key must be set for Docker deployment
# Set KPUBDATA_BUILDER_API_KEY via docker run -e or docker-compose
# The service will reject requests without proper authentication

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen(f'http://localhost:${KPUBDATA_BUILDER_PORT}/version')" || exit 1

# Run the Builder service
CMD ["kpubdata-builder", "serve"]
