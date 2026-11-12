"""Errors as RFC 9457 problem details (application/problem+json)."""

from http import HTTPStatus
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

log = structlog.get_logger()

PROBLEM_JSON = "application/problem+json"


class FieldError(BaseModel):
    field: str
    message: str


class Problem(BaseModel):
    """Shape of every error response (RFC 9457). Used for the API documentation."""

    type: str
    title: str
    status: int
    code: str = Field(description="Stable identifier to branch on. See docs/errors.md.")
    detail: str
    instance: str
    errors: list[FieldError] | None = Field(
        default=None, description="Present on validation errors."
    )


# Applied to every route so the docs show the real error format, not FastAPI's default.
PROBLEM_RESPONSES: dict[int | str, dict[str, Any]] = {
    422: {"model": Problem, "description": "The request is not valid"},
}


def use_problem_media_type(schema: dict[str, Any]) -> dict[str, Any]:
    """FastAPI documents response models as application/json; ours are problem+json."""
    for path in schema.get("paths", {}).values():
        for operation in path.values():
            for response in operation.get("responses", {}).values():
                content = response.get("content", {})
                ref = content.get("application/json", {}).get("schema", {}).get("$ref", "")
                if ref.endswith("/Problem"):
                    content[PROBLEM_JSON] = content.pop("application/json")
    return schema


class ProblemError(Exception):
    """Raise from anywhere to return a problem response.

    `code` is a stable, machine-readable identifier clients can branch on.
    """

    def __init__(
        self,
        status: int,
        code: str,
        detail: str,
        *,
        headers: dict[str, str] | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.headers = headers
        self.extra = extra


def _problem(
    request: Request,
    status: int,
    code: str,
    detail: str,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"https://github.com/tiff-xt/lobbyd/blob/main/docs/errors.md#{code}",
        "title": HTTPStatus(status).phrase,
        "status": status,
        "code": code,
        "detail": detail,
        "instance": request.url.path,
        **extra,
    }
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProblemError)
    async def _problem_error(request: Request, exc: ProblemError) -> JSONResponse:
        return _problem(request, exc.status, exc.code, exc.detail, exc.headers, **exc.extra)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Only location and message: the raw input may contain a password.
        errors = [
            {"field": ".".join(str(part) for part in e["loc"][1:]), "message": e["msg"]}
            for e in exc.errors()
        ]
        return _problem(
            request, 422, "validation_error", "The request is not valid.", errors=errors
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = HTTPStatus(exc.status_code).phrase.lower().replace(" ", "_")
        headers = dict(exc.headers) if exc.headers else None
        return _problem(request, exc.status_code, code, str(exc.detail), headers)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error")
        return _problem(request, 500, "internal_error", "Something went wrong on our side.")
