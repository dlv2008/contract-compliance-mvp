from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.routes.health import router as health_router
from app.routes.ui import router as ui_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="Contract Compliance MVP",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )
    app.include_router(health_router, prefix="/api")
    app.include_router(ui_router)
    app.mount("/static", StaticFiles(directory="app/static"), name="static")
    return app


app = create_app()
