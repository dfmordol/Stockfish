# Stockfish Engine Server

A FastAPI wrapper around the Stockfish engine that implements the
[`chess_explainer/engine_server/ENDPOINTS.md`]
API contract. Drop-in compatible with the existing Lc0-based engine server:
same routes, same request/response shapes.

## Layout

| File | Purpose |
| ---- | ------- |
| `config.py` | Paths & tunables (env-overridable) |
| `engine_manager.py` | Pool of Stockfish processes (`analyse`/`play`/`acquire_engine`) |
| `main.py` | FastAPI app exposing the 5 endpoints |
| `requirements.txt` | Python dependencies |

## Setup

1. Build the engine (from the repo root):

   ```sh
   cd src && make -j build ARCH=apple-silicon   # or your ARCH
   ```

   This produces `src/stockfish`, which `config.ENGINE_PATH` points to by default.

2. Install Python deps:

   ```sh
   pip install -r server/requirements.txt
   ```

3. Run:

   ```sh
   python server/main.py            # binds 0.0.0.0:8100
   # or: uvicorn main:app --host 0.0.0.0 --port 8100  (from inside server/)
   ```

## Endpoints

`GET /info`, `POST /analyze`, `POST /play`, `POST /wdl`, `POST /analyze/stream`.
See `ENDPOINTS.md` for full request/response schemas. Interactive docs at
`http://localhost:8100/docs`.

## Stockfish-specific notes

The contract was designed against Lc0; the behavioural mapping is:

- **WDL** comes from Stockfish's native `UCI_ShowWDL` output (always enabled),
  so `/wdl` and `/analyze/stream` report `source: "engine_native"`. If a
  position yields no WDL, the score-based `sf16.1` model is used
  (`source: "score_fallback"`).
- **`skill_level`** (`middle`/`advance`/`pro`) maps to a target Elo
  (1200/1800/2600; others → 2000) applied via `UCI_LimitStrength` + `UCI_Elo`.
  Stockfish clamps Elo to **[1320, 3190]**, so `middle` (1200) is raised to 1320.
- **`model_path`** is `null` — Stockfish uses its built-in NNUE network, so no
  external weights file is needed.

## Configuration (environment variables)

| Var | Default | Meaning |
| --- | ------- | ------- |
| `ENGINE_PATH` | `../src/stockfish` | Path to the Stockfish binary |
| `ENGINE_POOL_SIZE` | `3` | Concurrent engine processes |
| `ENGINE_THREADS` | `1` | `Threads` per engine |

> **CPU cap:** total worst-case CPU = `ENGINE_POOL_SIZE * ENGINE_THREADS` cores.
> The defaults (3 × 1) cap usage at **3 cores** while allowing 3 concurrent
> requests. Adjust either variable to change the cap.
| `ENGINE_HASH` | `128` | `Hash` (MB) per engine |
| `ENGINE_CACHE_PATH` | `./engine_cache.pkl` | Analysis cache file (empty to disable) |
| `HOST` / `PORT` | `0.0.0.0` / `8100` | Server bind address |
