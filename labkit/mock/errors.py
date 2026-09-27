"""API-shaped errors raised by the mock.  They serialize exactly like the real API's
error envelope, so the Anthropic SDK raises its normal typed exceptions
(`anthropic.BadRequestError`, `anthropic.NotFoundError`, `anthropic.RateLimitError`, ...)."""

from __future__ import annotations

ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    500: "api_error",
    529: "overloaded_error",
}


class ApiError(Exception):
    def __init__(self, status: int, message: str, err_type: str | None = None,
                 headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.err_type = err_type or ERROR_TYPES.get(status, "api_error")
        self.headers = headers or {}

    def body(self, request_id: str) -> dict:
        return {"type": "error", "error": {"type": self.err_type, "message": self.message}, "request_id": request_id}


def bad_request(message: str) -> ApiError:
    return ApiError(400, message)
