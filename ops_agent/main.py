from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Security
from fastapi.openapi.utils import get_openapi
from fastapi.security import APIKeyHeader
from .replication_health import router as replication_router
from .middleware import ApiKeyMiddleware

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

app = FastAPI(
    title="eFiche Ops Agent",
    version="2.0.0",
)

app.add_middleware(ApiKeyMiddleware)
app.include_router(replication_router)


@app.get("/health")
async def health():
    return {"status": "ok"}


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        routes=app.routes,
    )
    schema["components"]["securitySchemes"] = {
        "ApiKeyHeader": {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
        }
    }
    for path in schema["paths"].values():
        for operation in path.values():
            operation["security"] = [{"ApiKeyHeader": []}]
    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi