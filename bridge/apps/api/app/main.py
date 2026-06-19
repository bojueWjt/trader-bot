from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI, Request

from app.audit.router import router as audit_router
from app.reports.router import router as reports_router
from app.dashboard.freqtrade_router import router as freqtrade_router
from app.dashboard.router import router as dashboard_router
from app.dashboard.system_router import router as system_router
from app.risk.router import router as risk_router
from app.security.auth import router as auth_router
from app.security.release_gates_router import router as release_gates_router
from app.db.repositories_signal import SignalRepository
from app.security.router import router as security_router
from app.services.message_processing import build_message_processing_store
from app.services.signal_store import SignalStore
from app.signals.router import router as signals_router
from app.settings import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name)
    app.state.message_processing_store = build_message_processing_store(settings.hermes_signal_store_url)
    app.state.signal_store = SignalStore(repository=SignalRepository(settings.signal_store_url))
    _install_request_id_middleware(app)
    app.include_router(signals_router, prefix="/api/signals", tags=["signals"])
    app.include_router(dashboard_router, prefix="/api/dashboard", tags=["dashboard"])
    app.include_router(reports_router, prefix="/api/reports", tags=["reports"])
    app.include_router(risk_router, prefix="/api/risk", tags=["risk"])
    app.include_router(audit_router, prefix="/api/audit", tags=["audit"])
    app.include_router(auth_router, prefix="/auth", tags=["auth"])
    app.include_router(auth_router, prefix="/api/auth", tags=["auth"])
    app.include_router(security_router, prefix="/api/security", tags=["security"])
    app.include_router(release_gates_router, prefix="/api/release-gates", tags=["release-gates"])
    app.include_router(system_router, prefix="/api/system", tags=["system"])
    app.include_router(freqtrade_router, prefix="/api/freqtrade", tags=["freqtrade"])

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


def _install_request_id_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def add_request_id(request: Request, call_next):
        request_id = request.headers.get("x-request-id")
        if not request_id:
            request_id = str(uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response


app = create_app()
