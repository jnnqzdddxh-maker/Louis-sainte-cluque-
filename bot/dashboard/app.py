"""Dashboard de surveillance — app web légère (section 6).

Affiche : positions ouvertes, scores en direct des candidats, log des
décisions. Héberge aussi le webhook Helius (wallets Solana suivis) puisque
c'est le point d'entrée temps réel qui alimente le wallet tracker.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from core.engine import TradingEngine
from connectors.wallet_tracker import parse_helius_webhook_payload

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(engine: TradingEngine, tracked_solana_wallets: set[str] | None = None) -> FastAPI:
    app = FastAPI(title="Bot de trading — dashboard de surveillance")
    app.state.engine = engine
    app.state.tracked_solana_wallets = tracked_solana_wallets or set()

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/status")
    def status():
        rg = engine.risk_guard
        return {
            "dry_run": engine.dry_run,
            "halted": rg.halted,
            "halt_reason": rg.halt_reason,
            "daily_pnl_eur": rg.daily_pnl_eur,
            "open_positions": sum(1 for p in engine.position_manager.positions.values() if not p.closed),
            "max_open_positions": engine.cfg["risk"]["max_open_positions"],
        }

    @app.get("/api/positions")
    def positions():
        return [
            {**asdict(p), "confidence": p.confidence.value}
            for p in engine.position_manager.positions.values()
        ]

    @app.get("/api/candidates")
    def candidates():
        out = []
        for c in engine.candidates.values():
            out.append(
                {
                    "token_address": c.token_address,
                    "chain": c.chain,
                    "scored_at": c.scored_at.isoformat(),
                    "total_score": c.score.total_score,
                    "confidence": c.score.confidence.value,
                    "wallet_subscore": c.score.wallet_subscore,
                    "market_subscore": c.score.market_subscore,
                    "twitter_subscore": c.score.twitter_subscore,
                    "rejected_anti_rug": c.score.rejected_anti_rug,
                    "rejection_reason": c.score.rejection_reason,
                }
            )
        return sorted(out, key=lambda r: r["total_score"], reverse=True)

    @app.get("/api/decisions")
    def decisions(limit: int = 200):
        return engine.logger.read_all(limit=limit)

    @app.post("/api/risk/reset")
    async def reset_risk(request: Request):
        body = await request.json()
        confirmed_by = body.get("confirmed_by", "")
        try:
            engine.risk_guard.manual_reset(confirmed_by)
        except Exception as exc:  # ManualResetRequiredError
            return JSONResponse({"error": str(exc)}, status_code=400)
        engine.logger.log("risk_manual_reset", confirmed_by=confirmed_by)
        return {"ok": True}

    @app.post("/webhooks/helius")
    async def helius_webhook(request: Request):
        payload = await request.json()
        events = parse_helius_webhook_payload(payload, app.state.tracked_solana_wallets)
        for event in events:
            engine.on_wallet_buy_event(event)
        return {"received": len(events)}

    return app
