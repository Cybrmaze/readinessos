"""
ReadinessOS Facility Compliance -- standalone FastAPI app. Runs as its own
systemd unit (readinessos-api.service), own OS user (readinessos), own
port, own database (readinessos). No import from, or network path to, the
CLG facility platform at /opt/clg or the Business Formation service at
/opt/clg-bf -- same isolation principle applied to a third product.

Sold commercially alongside JUSTINE (bundled at the pricing/marketing
layer only) -- architecturally fully independent, matching how HELIOS,
POLLY, JUSTINE, and NEXA are all "sold together" as avatars yet remain
isolated products under /opt/clg.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.activities_routes import router as activities_router
from api.admin_routes import router as admin_router
from api.auth_routes import router as auth_router
from api.bridge_routes import router as bridge_router
from api.facility_routes import router as facility_router
from api.submissions_routes import router as submissions_router
from api.users_routes import router as users_router
from services.database import close_pool, init_pool

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()
    yield
    await close_pool()


app = FastAPI(title="ReadinessOS Facility Compliance", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://co.myclg.net"],
    allow_methods=["GET", "POST", "PATCH", "PUT"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(auth_router)
app.include_router(activities_router)
app.include_router(facility_router)
app.include_router(submissions_router)
app.include_router(users_router)
app.include_router(admin_router)
app.include_router(bridge_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
