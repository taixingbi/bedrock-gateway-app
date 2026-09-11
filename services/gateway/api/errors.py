"""Shared error-response builder for every route module (public + admin)."""
from __future__ import annotations

from starlette.responses import JSONResponse

from .schemas import ErrorBody, ErrorResponse


def error_response(status_code: int, code: str, message: str, request_id: str) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(code=code, message=message, request_id=request_id))
    return JSONResponse(body.model_dump(), status_code=status_code)
