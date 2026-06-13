"""Domain errors that map to the project's error envelope.

Each maps to an HTTP status + ``error.code`` via handlers registered in
``app/main.py``. Messages are safe to surface to clients (no internal detail,
no PII).
"""


class NotFoundError(Exception):
    """A requested resource does not exist. Maps to 404 / ``not_found``."""

    def __init__(self, message: str = "Resource not found.") -> None:
        self.message = message
        super().__init__(message)


class ConflictError(Exception):
    """A request conflicts with current state. Maps to 409 / ``conflict``."""

    def __init__(self, message: str = "Resource conflict.") -> None:
        self.message = message
        super().__init__(message)


class ServiceError(Exception):
    """An upstream/dependency failure (e.g. the Supabase Auth Admin API) or an
    operational misconfiguration. Maps to 502 / ``service_unavailable``.

    The message is a generic, client-safe summary — it never carries the
    upstream response body or any secret."""

    def __init__(self, message: str = "An upstream service is unavailable.") -> None:
        self.message = message
        super().__init__(message)
