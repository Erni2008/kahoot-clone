from __future__ import annotations

import secrets
import time
import uuid

from fastapi import HTTPException, Request

from .access import AccessStore, Principal, role_allows


def extract_bearer_token(request: Request) -> str:
    authorization = str(request.headers.get("authorization") or "")
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    return token or str(
        request.headers.get("x-editor-token") or request.query_params.get("token") or ""
    ).strip()


def authenticate_request(
    request: Request,
    *,
    owner_token: str,
    access_store: AccessStore,
    required_role: str = "viewer",
) -> Principal:
    token = extract_bearer_token(request)
    if token and secrets.compare_digest(token, owner_token):
        principal = Principal(id="owner", name="Владелец", role="owner", owner=True)
    else:
        principal = access_store.authenticate(token)
    if principal is None:
        raise HTTPException(status_code=401, detail="Неверный или отозванный ключ доступа")
    if not role_allows(principal.role, required_role):
        raise HTTPException(status_code=403, detail="Для этого действия недостаточно прав")
    return principal


async def production_headers(request: Request, call_next):
    started = time.perf_counter()
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    if request.url.path.startswith("/static/studio/assets/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    if forwarded_proto == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Server-Timing"] = f'app;dur={(time.perf_counter() - started) * 1000:.1f}'
    return response
