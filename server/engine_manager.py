import asyncio
import copy
import logging
import os
import pickle
from contextlib import asynccontextmanager
from typing import List, Optional

import chess
import chess.engine
from chess import polyglot


class EngineManager:
    """Manages a pool of Stockfish engines for concurrent analysis.

    Mirrors the interface expected by the engine-server API (see ENDPOINTS.md):
    ``analyse``, ``play``, ``acquire_engine`` and ``restart_engine_by_error``.
    Unlike the Lc0 variant, Stockfish needs no external weights file; it exposes
    Win/Draw/Loss via the ``UCI_ShowWDL`` option and limits strength via
    ``UCI_LimitStrength`` / ``UCI_Elo``.
    """

    def __init__(
        self,
        engine_path: str,
        pool_size: int = 2,
        cache_file: Optional[str] = "engine_cache.pkl",
        model_path: Optional[str] = None,
        threads: int = 1,
        hash_mb: int = 128,
    ):
        self.engine_path = engine_path
        self.model_path = model_path  # unused for Stockfish; kept for parity
        self.pool_size = pool_size
        self.threads = threads
        self.hash_mb = hash_mb
        self.engines: List[chess.engine.UciProtocol] = []
        self.locks: List[asyncio.Lock] = []
        self.analyse_cache = {}
        self.cache_file = cache_file
        if self.cache_file:
            self.load_cache(self.cache_file)
        self.initialized = False

    # ------------------------------------------------------------------ cache
    def load_cache(self, file_path: str):
        if os.path.exists(file_path):
            try:
                with open(file_path, "rb") as f:
                    self.analyse_cache = pickle.load(f)
                logging.info(f"Loaded {len(self.analyse_cache)} items from cache: {file_path}")
            except Exception as e:
                logging.error(f"Failed to load cache from {file_path}: {e}")

    def save_cache(self, file_path: str = None):
        target_path = file_path or self.cache_file
        if target_path:
            try:
                with open(target_path, "wb") as f:
                    pickle.dump(self.analyse_cache, f)
                logging.info(f"Saved {len(self.analyse_cache)} items to cache: {target_path}")
            except Exception as e:
                logging.error(f"Failed to save cache to {target_path}: {e}")

    # ----------------------------------------------------------- engine config
    def _base_options(self) -> dict:
        return {
            "Threads": self.threads,
            "Hash": self.hash_mb,
            "UCI_ShowWDL": True,
        }

    async def _spawn_engine(self) -> chess.engine.UciProtocol:
        _transport, engine = await chess.engine.popen_uci(self.engine_path)
        await engine.configure(self._base_options())
        return engine

    async def initialize(self):
        if self.initialized:
            return
        logging.info(f"Initializing {self.pool_size} Stockfish engine(s)...")
        for i in range(self.pool_size):
            try:
                engine = await self._spawn_engine()
                self.engines.append(engine)
                self.locks.append(asyncio.Lock())
                logging.info(f"Engine {i + 1}/{self.pool_size} initialized")
            except Exception as e:
                logging.error(f"Failed to initialize engine {i + 1}: {e}")
                raise
        self.initialized = True
        logging.info("All engines initialized successfully")

    async def shutdown(self):
        if self.cache_file:
            self.save_cache(self.cache_file)
        logging.info("Shutting down engines...")
        for i, engine in enumerate(self.engines):
            try:
                await engine.quit()
            except Exception as e:
                logging.error(f"Error shutting down engine {i + 1}: {e}")
        self.engines.clear()
        self.locks.clear()
        self.initialized = False

    async def restart_engine_by_error(self):
        """Restart the first unlocked engine that may be in a bad state."""
        for i, lock in enumerate(self.locks):
            if not lock.locked():
                try:
                    logging.info(f"Restarting engine {i + 1}...")
                    try:
                        await self.engines[i].quit()
                    except Exception:
                        pass
                    self.engines[i] = await self._spawn_engine()
                    logging.info(f"Engine {i + 1} restarted successfully")
                    return
                except Exception as e:
                    logging.error(f"Failed to restart engine {i + 1}: {e}")
                    raise

    # --------------------------------------------------------------- pool use
    @asynccontextmanager
    async def acquire_engine(self):
        """Acquire an available engine from the pool, waiting if all are busy."""
        if not self.initialized:
            await self.initialize()

        while True:
            for i, lock in enumerate(self.locks):
                if lock.locked():
                    continue
                await lock.acquire()
                try:
                    yield self.engines[i]
                finally:
                    lock.release()
                return
            await asyncio.sleep(0.05)

    # ----------------------------------------------------------------- queries
    async def analyse(self, board: chess.Board, limit: chess.engine.Limit, **kwargs):
        """Analyze a position, caching by (zobrist hash, time, multipv)."""
        multipv = kwargs.get("multipv", 1)
        zb_hash = polyglot.zobrist_hash(board)

        cached_entry = self.analyse_cache.get(zb_hash)
        if cached_entry:
            cached_limit = cached_entry.get("limit")
            cached_multipv = cached_entry.get("multipv", 1)
            if (
                cached_limit
                and cached_limit.time
                and limit.time
                and cached_limit.time >= limit.time
                and cached_multipv >= multipv
            ):
                info = cached_entry["info"]
                if isinstance(info, list) and len(info) > multipv:
                    return copy.deepcopy(info[:multipv])
                return copy.deepcopy(info)

        async with self.acquire_engine() as engine:
            info = await engine.analyse(board, limit, **kwargs)
            self.analyse_cache[zb_hash] = {"info": info, "limit": limit, "multipv": multipv}
            return copy.deepcopy(info)

    async def play(self, board: chess.Board, limit: chess.engine.Limit, **kwargs):
        """Get the engine's chosen move for a position."""
        async with self.acquire_engine() as engine:
            return await engine.play(board, limit, **kwargs)
