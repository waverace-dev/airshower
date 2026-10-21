"""Stable errors shared by the reference store and HTTP transport."""


class APIError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def invalid(message="Invalid request."):
    return APIError(400, "invalid_request", message)


def forbidden(message="This action is not permitted."):
    return APIError(403, "forbidden", message)


def missing():
    return APIError(404, "not_found", "Resource not found.")


def conflict(message):
    return APIError(409, "conflict", message)
