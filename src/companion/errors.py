"""One error shape for the HTTP API: a real status code plus
{"error": {"code": "...", "message": "...", "details": {...}}}.

v1 returned {"error": "..."} with HTTP 200 from seven endpoints, which
is invisible to every client, proxy, and monitor that keys on status
(and made the JS check `data.error` on a success response). Every
endpoint now raises ApiError; the handler renders the envelope. The
pre-existing HTTPException(400/404) on the PDF route renders the same
shape, so clients see one format.
"""
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse


class ApiError(HTTPException):
    def __init__(self, status_code: int, code: str, message: str, details: dict | None = None):
        super().__init__(status_code=status_code, detail=message)
        self.code = code
        self.message = message
        self.details = details or {}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        body = {"error": {"code": exc.code, "message": exc.message}}
        if exc.details:
            body["error"]["details"] = exc.details
        return JSONResponse(status_code=exc.status_code, content=body)

    # A request that stops at the outbound boundary is an answer, never a 500: chat maps these itself, and every other
    # route (drafting, resume tailoring) would otherwise leak a stack trace for a policy decision.
    def _boundary(status: int, code: str):
        async def handler(_: Request, exc: Exception) -> JSONResponse:
            return JSONResponse(status_code=status, content={"error": {"code": code, "message": str(exc)}})
        return handler

    from companion.llm import ProviderUnavailable
    from companion.outbound import ReleaseRefused
    from companion.provider import AuditUnavailable

    app.add_exception_handler(ReleaseRefused, _boundary(403, "release_refused"))
    app.add_exception_handler(AuditUnavailable, _boundary(503, "audit_unavailable"))
    app.add_exception_handler(ProviderUnavailable, _boundary(503, "provider_unavailable"))

    @app.exception_handler(HTTPException)
    async def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
        code = {400: "bad_request", 404: "not_found", 422: "invalid"}.get(exc.status_code, "error")
        return JSONResponse(status_code=exc.status_code, content={"error": {"code": code, "message": str(exc.detail)}})
