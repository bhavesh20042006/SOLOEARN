# ── Stage 1: Builder ─────────────────────────────────────────────
FROM python:3.14-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2: Runtime ─────────────────────────────────────────────
FROM python:3.14-slim AS runtime

# Create non-root user
RUN groupadd --gid 1000 soloearn \
    && useradd --uid 1000 --gid soloearn --shell /bin/bash --create-home soloearn

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code (only necessary files)
COPY core/           ./core/
COPY scripts/        ./scripts/
COPY landing_page/   ./landing_page/
COPY .streamlit/     ./.streamlit/
COPY app.py          ./app.py
COPY requirements.txt ./requirements.txt

# Create data directory with correct ownership
RUN mkdir -p /app/data /app/workspaces \
    && chown -R soloearn:soloearn /app

# Switch to non-root user
USER soloearn

# Expose Streamlit default port
EXPOSE 8501

# Healthcheck – Streamlit serves on /healthz by default
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health')" || exit 1

# Launch Streamlit
CMD ["streamlit", "run", "app.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
