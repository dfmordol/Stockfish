# syntax=docker/dockerfile:1

# =============================================================================
# Stage 1 — compile the Stockfish engine (NNUE net is embedded in the binary)
# =============================================================================
FROM gcc:13-bookworm AS engine-build

# ARCH for `make build`. Leave empty to auto-detect the builder's native CPU
# (best perf when the image runs on the same host/arch). For portable x86-64
# images set e.g. SF_ARCH=x86-64-sse41-popcnt; for ARM set SF_ARCH=armv8.
ARG SF_ARCH=

# net.sh needs curl (or wget) + ca-certificates to fetch the NNUE network.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /sf
# scripts/ holds net.sh; any pre-downloaded src/*.nnue is validated and reused.
COPY scripts/ ./scripts/
COPY src/ ./src/

RUN cd src \
    && if [ -n "$SF_ARCH" ]; then make -j"$(nproc)" build ARCH="$SF_ARCH"; \
       else make -j"$(nproc)" build; fi \
    && strip stockfish

# =============================================================================
# Stage 2 — runtime: FastAPI server wrapping the engine
# =============================================================================
FROM python:3.11-slim AS runtime

# libstdc++6 / libgcc-s1 are needed to run the compiled engine.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first for better layer caching.
COPY server/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Server code and the compiled engine binary.
COPY server/ ./server/
COPY --from=engine-build /sf/src/stockfish /usr/local/bin/stockfish

# Run as a non-root user; give it a writable data dir for cache/logs.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

ENV ENGINE_PATH=/usr/local/bin/stockfish \
    HOST=0.0.0.0 \
    PORT=8100 \
    ENGINE_POOL_SIZE=2 \
    ENGINE_THREADS=2 \
    ENGINE_HASH=128 \
    ENGINE_CACHE_PATH=/app/data/engine_cache.pkl

EXPOSE 8100
WORKDIR /app/server
CMD ["python", "main.py"]
