import os
import logging
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("efiche.ops.auth")

TIER_READ = "read"
TIER_WRITE = "write"

WRITE_ENDPOINTS = {
    "/restart-subscription",
    "/force-schema-sync",
    "/trigger-backfill",
    "/restart-container",
}


def _load_key_map():
    key_map = {}

    if (k := os.getenv("OPS_API_KEY_READ")):
        key_map[k] = {"engineer_id": "dev", "tier": TIER_READ}
    if (k := os.getenv("OPS_API_KEY_WRITE")):
        key_map[k] = {"engineer_id": "dev", "tier": TIER_WRITE}

    for i in range(1, 10):
        for tier, prefix in [(TIER_READ, "OPS_API_KEY_READ_"), (TIER_WRITE, "OPS_API_KEY_WRITE_")]:
            if (k := os.getenv(f"{prefix}{i}")):
                key_map[k] = {"engineer_id": str(i), "tier": tier}

    return key_map


class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path in ("/health", "/docs", "/openapi.json", "/redoc"):
            return await call_next(request)

        # Load key map on each request so dotenv values are always picked up
        key_map = _load_key_map()

        api_key = request.headers.get("X-API-Key")
        if not api_key or api_key not in key_map:
            logger.warning("Unauthorized request to %s", request.url.path)
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        key_info = key_map[api_key]
        requires_write = any(request.url.path.startswith(ep) for ep in WRITE_ENDPOINTS)
        if requires_write and key_info["tier"] != TIER_WRITE:
            return JSONResponse({"error": "forbidden: write-tier key required"}, status_code=403)

        request.state.engineer_id = key_info["engineer_id"]
        request.state.tier = key_info["tier"]
        return await call_next(request)