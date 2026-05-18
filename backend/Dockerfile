# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# System deps needed to compile some Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps into a virtual-env so we can copy them cleanly
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
# Install the CPU-only torch FIRST (this VM has no GPU). The default torch
# wheel bundles ~3-5GB of NVIDIA CUDA/cuDNN libs that are pure dead weight
# here — they bloated the image, blew out the disk on every build, and
# tripled build time. Pre-installing CPU torch means sentence-transformers
# sees torch already satisfied and won't pull the CUDA wheel.
RUN pip install --upgrade pip && \
    pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt && \
    # python-jobspy is installed --no-deps to bypass its strict NUMPY pin
    # which conflicts with sentence-transformers / spacy / pandas.  Its
    # runtime deps (bs4, requests, pydantic, pandas, numpy, tls-client)
    # are already in requirements.txt above.
    pip install --no-cache-dir --no-deps python-jobspy==1.1.82

# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    postgresql-client \
    curl \
    gzip \
    # Playwright/Chromium runtime libs (auto-apply agent)
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    libxshmfence1 libgtk-3-0 fonts-liberation libappindicator3-1 xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual-env from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Copy application source
COPY . .

# Create log / upload / data / backup dirs with correct ownership
RUN mkdir -p /app/logs /app/uploads /app/data /app/backups && chown -R appuser:appuser /app

USER appuser

# Health-check via the /api/health endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

EXPOSE 8000

# Default: run the FastAPI server.
# Celery worker/beat are started as separate Compose services
# by overriding CMD.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
