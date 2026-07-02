import os

# Path to the Stockfish executable. Defaults to the binary produced by
# `make build` in ../src. Override with the ENGINE_PATH environment variable.
_DEFAULT_ENGINE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "src", "stockfish")
)
ENGINE_PATH = os.getenv("ENGINE_PATH", _DEFAULT_ENGINE)

# Stockfish uses a built-in NNUE network (no external weights file needed),
# so MODEL_PATH is None. Kept for API compatibility with the endpoint spec.
MODEL_PATH = os.getenv("MODEL_PATH") or None

# Number of engine processes kept in the pool for concurrent requests.
# CPU is capped at 3 cores: POOL_SIZE * ENGINE_THREADS <= 3 (3 engines x 1
# thread = at most 3 concurrent single-threaded searches).
POOL_SIZE = int(os.getenv("ENGINE_POOL_SIZE", "3"))

# Threads / Hash (MB) configured on every engine in the pool.
ENGINE_THREADS = int(os.getenv("ENGINE_THREADS", "1"))
ENGINE_HASH = int(os.getenv("ENGINE_HASH", "128"))

# Cache file for analysis results (set to empty string to disable).
ENGINE_CACHE_PATH = os.getenv("ENGINE_CACHE_PATH", "./engine_cache.pkl") or None

# Server bind address.
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8100"))
