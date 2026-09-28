from fastapi.responses import JSONResponse
from web.public_safety import public_error_message


def unavailable(**fields):
    return JSONResponse(status_code=503, content={"success": False, "error": public_error_message(), **fields})
