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
