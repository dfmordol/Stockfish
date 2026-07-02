import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Optional

import chess
import chess.engine
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import config
from engine_manager import EngineManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("engine_server.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# Stockfish UCI_Elo bounds (see src/search.h: LowestElo / HighestElo).
ELO_MIN, ELO_MAX = 1320, 3190

# skill_level -> target Elo, per ENDPOINTS.md (other values default to 2000).
WDL_CALIBRATION_ELO = {
    "middle": 1200,
    "advance": 1800,
    "pro": 2600,
}

manager: Optional[EngineManager] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global manager
    logger.info(f"Initializing engine manager with path: {config.ENGINE_PATH}")
    manager = EngineManager(
        config.ENGINE_PATH,
        pool_size=config.POOL_SIZE,
        cache_file=config.ENGINE_CACHE_PATH,
        model_path=config.MODEL_PATH,
        threads=config.ENGINE_THREADS,
        hash_mb=config.ENGINE_HASH,
    )
    await manager.initialize()
    yield
    logger.info("Shutting down engine manager...")
    await manager.shutdown()


app = FastAPI(lifespan=lifespan)


class AnalyzeRequest(BaseModel):
    fen: str
    time_limit: float = 0.1
    multipv: int = 1


class PlayRequest(BaseModel):
    fen: str
    time_limit: float = 0.1


class WdlRequest(BaseModel):
    fen: str
    skill_level: str = "advance"
    time_limit: float = 0.5


class StreamAnalyzeRequest(BaseModel):
    fen: str
    time_limit: float = 5.0
    multipv: int = 1
    interval: float = 1.0


# In-memory WDL cache: (fen, skill_level) -> response dict
wdl_cache = {}


@app.get("/info")
async def get_info():
    return {
        "status": "running",
        "engine_path": config.ENGINE_PATH,
        "model_path": config.MODEL_PATH,
    }


def _serialize_score(score):
    if score.is_mate():
        return {"mate": score.mate()}
    return {"cp": score.score()}


def serialize_info(result):
    """Serialize a single chess.engine info dict for the /analyze response."""
    out = {}
    if "score" in result:
        score_obj = result["score"]
        out["score"] = {
            "white": _serialize_score(score_obj.white()),
            "relative": _serialize_score(score_obj.relative),
        }
    if "pv" in result:
        out["pv"] = [move.uci() for move in result["pv"]]
    for key in ["depth", "seldepth", "nodes", "nps", "tbhits", "hashfull", "time", "multipv"]:
        if key in result:
            out[key] = result[key]
    return out


@app.post("/analyze")
async def analyze(request: AnalyzeRequest):
    try:
        board = chess.Board(request.fen)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid FEN")

    limit = chess.engine.Limit(time=request.time_limit)
    try:
        result = await manager.analyse(board, limit, multipv=request.multipv)
    except Exception as e:
        logger.error(f"Analysis error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if request.multipv > 1:
        if isinstance(result, list):
            return [serialize_info(r) for r in result]
        if hasattr(result, "__iter__") and not isinstance(result, dict):
            return [serialize_info(r) for r in result]
        return [serialize_info(result)]

    # Single PV: normalize any list/iterable to the first entry.
    if isinstance(result, list):
        return serialize_info(result[0]) if result else {}
    if hasattr(result, "__iter__") and not isinstance(result, dict):
        first = next(iter(result), None)
        return serialize_info(first) if first is not None else {}
    return serialize_info(result)


@app.post("/play")
async def play(request: PlayRequest):
    try:
        board = chess.Board(request.fen)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid FEN")

    limit = chess.engine.Limit(time=request.time_limit)
    try:
        result = await manager.play(board, limit)
    except Exception as e:
        logger.error(f"Play error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "move": result.move.uci() if result.move else None,
        "ponder": result.ponder.uci() if result.ponder else None,
    }


@app.post("/wdl")
async def get_wdl(request: WdlRequest):
    try:
        board = chess.Board(request.fen)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid FEN")

    cache_key = (request.fen, request.skill_level)
    if cache_key in wdl_cache:
        return wdl_cache[cache_key]

    calibration_elo = WDL_CALIBRATION_ELO.get(request.skill_level, 2000)
    clamped_elo = max(ELO_MIN, min(ELO_MAX, calibration_elo))
    limit = chess.engine.Limit(time=request.time_limit)

    try:
        async with manager.acquire_engine() as engine:
            # Limit strength for this request so the WDL reflects the target Elo.
            await engine.configure({"UCI_LimitStrength": True, "UCI_Elo": clamped_elo})
            try:
                result = await engine.analyse(board, limit, info=chess.engine.INFO_ALL)
            finally:
                await engine.configure({"UCI_LimitStrength": False})

        # Stockfish provides native WDL from White's perspective via UCI_ShowWDL.
        if isinstance(result, dict) and result.get("wdl") is not None:
            white_wdl = result["wdl"].white()
            resp = {
                "white": round(white_wdl.wins / 1000, 3),
                "draw": round(white_wdl.draws / 1000, 3),
                "black": round(white_wdl.losses / 1000, 3),
                "calibration_elo": calibration_elo,
                "source": "engine_native",
            }
            wdl_cache[cache_key] = resp
            return resp

        # Fallback: derive WDL from the score using the sf16.1 model.
        score = result.get("score") if isinstance(result, dict) else None
        if not score:
            return {"white": 0.33, "draw": 0.34, "black": 0.33}

        wdl = score.white().wdl(model="sf16.1", ply=board.ply())
        resp = {
            "white": round(wdl.wins / 1000, 3),
            "draw": round(wdl.draws / 1000, 3),
            "black": round(wdl.losses / 1000, 3),
            "calibration_elo": calibration_elo,
            "source": "score_fallback",
        }
        wdl_cache[cache_key] = resp
        return resp
    except Exception as e:
        logger.error(f"WDL error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _info_to_dict(info, board, is_final=False):
    """Convert a chess.engine info dict to a JSON-serializable stream line."""
    result = {"final": is_final}

    if "score" in info:
        white = info["score"].white()
        if white.is_mate():
            result["score"] = {"white": {"mate": white.mate()}}
        else:
            result["score"] = {"white": {"cp": white.score()}}

        # Prefer native WDL; fall back to the score-based model.
        if info.get("wdl") is not None:
            w = info["wdl"].white()
        else:
            w = white.wdl(model="sf16.1", ply=board.ply())
        result["wdl"] = {
            "white": round(w.wins / 1000, 3),
            "draw": round(w.draws / 1000, 3),
            "black": round(w.losses / 1000, 3),
        }

    if "pv" in info:
        result["pv"] = [m.uci() for m in info["pv"][:10]]

    for key in ["depth", "seldepth", "nodes", "nps", "time"]:
        if key in info:
            result[key] = info[key]

    return result


@app.post("/analyze/stream")
async def analyze_stream(request: StreamAnalyzeRequest):
    """Stream intermediate analysis as NDJSON: a quick pass, then the final one."""
    try:
        board = chess.Board(request.fen)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid FEN")

    logger.info(
        f"analyse/stream: fen={request.fen[:40]} multipv={request.multipv} time={request.time_limit}"
    )

    async def generate():
        quick_time = min(0.3, request.time_limit / 2)
        passes = [(quick_time, False), (request.time_limit, True)]

        for t, is_final in passes:
            for attempt in range(2):
                try:
                    async with manager.acquire_engine() as eng:
                        result = await eng.analyse(
                            board, chess.engine.Limit(time=t), multipv=request.multipv
                        )
                    if not isinstance(result, list):
                        result = [result]
                    lines = [_info_to_dict(r, board, is_final=is_final) for r in result]
                    yield json.dumps({"lines": lines, "final": is_final}) + "\n"
                    break
                except Exception as e:
                    if attempt == 0 and "EngineStateException" in type(e).__name__:
                        logger.warning(f"Engine in bad state, reinitializing: {e}")
                        try:
                            await manager.restart_engine_by_error()
                        except Exception:
                            pass
                        continue
                    logger.error(f"Stream analyse error: {e}")
                    yield json.dumps({"error": str(e)}) + "\n"
                    return

    return StreamingResponse(generate(), media_type="application/x-ndjson")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT)
