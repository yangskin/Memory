from __future__ import annotations

from typing import Any


def ok_result(message: str = "ok", **data: Any) -> dict[str, Any]:
    return {"ok": True, "error": None, "message": message, **data}


def error_result(error: str, message: str, **data: Any) -> dict[str, Any]:
    return {"ok": False, "error": error, "message": message, **data}
