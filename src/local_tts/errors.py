from typing import Any


class TTSError(Exception):
    """Error with a stable machine-readable code, reported to clients as JSON."""

    def __init__(self, code: str, message: str, http_status: int = 400, **details: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.details}
