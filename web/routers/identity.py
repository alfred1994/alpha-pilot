"""同源工作区账号入口，与公开只读看板隔离。"""

import ipaddress
import json
import os
import sqlite3
from urllib.parse import urlsplit
import hmac

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from services import identity
from web.public_safety import is_production


router = APIRouter()
SESSION_COOKIE = "alphapilot_session"
COOKIE_PATH = "/api/workspace"
MAX_CREDENTIAL_BYTES = 4096


def _http_error(error: identity.IdentityError) -> HTTPException:
    headers = {"Cache-Control": "no-store"}
    if error.retry_after is not None:
        headers["Retry-After"] = str(error.retry_after)
    return HTTPException(error.status_code, detail=error.message, headers=headers)


def _canonical_origin(value: str) -> str:
    if any(character.isspace() for character in value):
        raise ValueError("invalid origin")
    parts = urlsplit(value)
    if (
        parts.scheme not in {"https", "http"} or not parts.hostname
        or parts.username or parts.password or parts.query or parts.fragment
        or parts.path not in {"", "/"}
    ):
        raise ValueError("invalid origin")
    hostname = parts.hostname.encode("idna").decode("ascii").lower()
    port = parts.port
    if ":" in hostname:
        hostname = f"[{hostname}]"
    port_suffix = "" if port is None or (parts.scheme, port) in {("http", 80), ("https", 443)} else f":{port}"
    return f"{parts.scheme}://{hostname}{port_suffix}"


def _expected_origin(request: Request) -> str:
    configured = os.environ.get("ALPHAPILOT_WORKSPACE_ORIGIN", "").strip()
    candidate = configured or f"{request.url.scheme}://{request.url.netloc}"
    try:
        return _canonical_origin(candidate)
    except (ValueError, UnicodeError):
        raise HTTPException(503, detail="工作区访问地址配置不可用") from None


def _validate_origin(request: Request) -> str:
    expected = _expected_origin(request)
    parts = urlsplit(expected)
    is_loopback = parts.hostname == "localhost"
    try:
        is_loopback = is_loopback or ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        pass
    if parts.scheme != "https" and (is_production() or not is_loopback):
        raise HTTPException(403, detail="账号操作需要 HTTPS 安全连接")
    origin = request.headers.get("origin")
    if origin is not None:
        try:
            matches = _canonical_origin(origin) == expected
        except (ValueError, UnicodeError):
            matches = False
        if not matches:
            raise HTTPException(403, detail="请在当前工作区页面完成操作")
    if request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
        raise HTTPException(403, detail="请在当前工作区页面完成操作")
    return expected


def _client_ip(request: Request) -> str:
    # 反向代理信任由 ASGI 服务器配置，不能直接信任客户端提供的转发头。
    return request.client.host if request.client else "unknown"


async def _json_fields(request: Request, fields: dict[str, int], *, allow_empty: bool = False) -> dict:
    if allow_empty and not request.headers.get("content-type"):
        async for chunk in request.stream():
            if chunk:
                raise HTTPException(422, detail="请提交 JSON 数据")
        return {}
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise HTTPException(422, detail="请提交 JSON 数据")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_CREDENTIAL_BYTES:
            raise HTTPException(422, detail="账号信息超出长度限制")
        body.extend(chunk)
    try:
        payload = json.loads(body) if body or not allow_empty else {}
    except (ValueError, RecursionError, UnicodeError):
        raise HTTPException(422, detail="账号信息格式不正确") from None
    if (
        not isinstance(payload, dict) or set(payload) != set(fields)
        or any(not isinstance(payload[key], str) or len(payload[key]) > maximum for key, maximum in fields.items())
    ):
        raise HTTPException(422, detail="提交字段不完整或超出长度限制")
    try:
        for value in payload.values():
            value.encode("utf-8")
    except UnicodeError:
        raise HTTPException(422, detail="提交内容包含无效字符") from None
    return payload


async def _credentials(request: Request) -> tuple[str, str]:
    payload = await _json_fields(request, {"email": 254, "password": 128})
    return payload["email"], payload["password"]


def _identity_call(function, *args):
    try:
        return function(*args)
    except identity.IdentityError as error:
        raise _http_error(error) from None
    except (sqlite3.DatabaseError, OSError):
        raise HTTPException(503, detail="账号服务暂时不可用，请稍后重试") from None


def _auth_payload(user: dict) -> dict:
    return {
        "user": {key: user[key] for key in ("id", "email", "plan", "email_verified")},
        "csrf_token": user["csrf_token"],
        "limits": identity.get_entitlements(user["plan"]),
    }


def _cookie_secure(request: Request) -> bool:
    return is_production() or _expected_origin(request).startswith("https://")


def _session_response(request: Request, session: identity.Session, status_code: int) -> JSONResponse:
    response = JSONResponse(_auth_payload(session.user), status_code=status_code, headers={"Cache-Control": "no-store"})
    response.set_cookie(
        SESSION_COOKIE, session.token, max_age=identity.SESSION_TTL_SECONDS,
        httponly=True, secure=_cookie_secure(request), samesite="lax", path=COOKIE_PATH,
    )
    return response


def _load_user(request: Request) -> dict:
    try:
        user = identity.get_session(request.cookies.get(SESSION_COOKIE))
    except (sqlite3.DatabaseError, OSError):
        raise HTTPException(503, detail="工作区暂时不可用，请稍后再试") from None
    if user is None:
        raise HTTPException(401, detail="请先登录工作区", headers={"Cache-Control": "no-store"})
    return user


def _limit_workspace_request(request: Request, user: dict, action: str) -> None:
    counted = getattr(request.state, "workspace_rate_scopes", set())
    if action in counted:
        return
    try:
        identity.consume_workspace_request(user["id"], action)
    except identity.IdentityError as error:
        raise _http_error(error) from None
    except (sqlite3.DatabaseError, OSError):
        raise HTTPException(503, detail="工作区暂时不可用，请稍后再试") from None
    request.state.workspace_rate_scopes = counted | {action}


def require_user(request: Request) -> dict:
    user = _load_user(request)
    _limit_workspace_request(request, user, "read")
    return user


def _require_mutation_user(request: Request, *, rate_limit: bool = True) -> dict:
    _validate_origin(request)
    user = _load_user(request)
    csrf_token = request.headers.get("x-csrf-token", "")
    if len(csrf_token) != 64 or not csrf_token.isascii() or not hmac.compare_digest(csrf_token, user["csrf_token"]):
        raise HTTPException(403, detail="会话校验失败，请刷新页面后重试")
    if rate_limit:
        _limit_workspace_request(request, user, "write")
    return user


def require_mutation_user(request: Request) -> dict:
    return _require_mutation_user(request)


def assert_verified_trading_user(user: dict) -> None:
    _identity_call(identity.assert_email_verified, user)


def require_verified_trading_user(request: Request) -> dict:
    user = require_mutation_user(request)
    assert_verified_trading_user(user)
    return user


def _require_logout_user(request: Request) -> dict:
    # 消耗业务额度不能阻止安全退出；同源、会话和 CSRF 检查仍全部执行。
    return _require_mutation_user(request, rate_limit=False)


@router.post("/auth/register", status_code=201)
async def register(request: Request):
    _validate_origin(request)
    email, password = await _credentials(request)
    try:
        session = await run_in_threadpool(identity.register, email, password, _client_ip(request))
    except identity.IdentityError as error:
        raise _http_error(error) from None
    except (sqlite3.DatabaseError, OSError):
        raise HTTPException(503, detail="注册服务暂时不可用，请稍后再试") from None
    return _session_response(request, session, 201)


@router.post("/auth/login")
async def login(request: Request):
    _validate_origin(request)
    email, password = await _credentials(request)
    try:
        session = await run_in_threadpool(identity.login, email, password, _client_ip(request))
    except identity.IdentityError as error:
        raise _http_error(error) from None
    except (sqlite3.DatabaseError, OSError):
        raise HTTPException(503, detail="登录服务暂时不可用，请稍后再试") from None
    return _session_response(request, session, 200)


@router.get("/auth/me")
def me(user: dict = Depends(require_user)):
    return JSONResponse(_auth_payload(user), headers={"Cache-Control": "no-store"})


@router.post("/auth/logout", status_code=204)
def logout(request: Request, user: dict = Depends(_require_logout_user)):
    try:
        identity.revoke_session(request.cookies.get(SESSION_COOKIE))
    except (sqlite3.DatabaseError, OSError):
        raise HTTPException(503, detail="退出服务暂时不可用，请稍后再试") from None
    return _empty_response(request, clear_cookie=True)


def _empty_response(request: Request, *, clear_cookie: bool = False) -> Response:
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    if clear_cookie:
        response.delete_cookie(SESSION_COOKIE, path=COOKIE_PATH, secure=_cookie_secure(request), httponly=True, samesite="lax")
    return response


@router.get("/auth/capabilities")
def capabilities():
    return JSONResponse(identity.capabilities(), headers={"Cache-Control": "no-store"})


@router.post("/auth/email-verification/request", status_code=202)
async def request_verification(request: Request, user: dict = Depends(require_mutation_user)):
    await _json_fields(request, {}, allow_empty=True)
    await run_in_threadpool(_identity_call, identity.request_email_verification, user["id"], request.cookies.get(SESSION_COOKIE), _client_ip(request))
    return JSONResponse({"detail": "验证邮件已发送，请检查收件箱。"}, status_code=202, headers={"Cache-Control": "no-store"})


@router.get("/auth/email-verification/status")
def verification_status(request: Request, user: dict = Depends(require_user)):
    result = _identity_call(identity.email_verification_status, user["id"], request.cookies.get(SESSION_COOKIE))
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/auth/email-verification/confirm")
async def confirm_verification(request: Request):
    _validate_origin(request)
    fields = await _json_fields(request, {"token": 128})
    await run_in_threadpool(_identity_call, identity.confirm_email_verification, fields["token"], _client_ip(request))
    return JSONResponse({"email_verified": True}, headers={"Cache-Control": "no-store"})


@router.post("/auth/password-reset/request", status_code=202)
async def request_password_reset(request: Request, background_tasks: BackgroundTasks):
    _validate_origin(request)
    fields = await _json_fields(request, {"email": 254})
    delivery = await run_in_threadpool(_identity_call, identity.prepare_password_reset, fields["email"], _client_ip(request))
    background_tasks.add_task(identity.deliver_password_reset, delivery)
    return JSONResponse(
        {"detail": "重置申请已受理；若该邮箱可用于恢复账号，请检查收件箱。未收到时可稍后重试。"},
        status_code=202, headers={"Cache-Control": "no-store"},
    )


@router.post("/auth/password-reset/confirm", status_code=204)
async def confirm_password_reset(request: Request):
    _validate_origin(request)
    fields = await _json_fields(request, {"token": 128, "new_password": 128})
    await run_in_threadpool(_identity_call, identity.confirm_password_reset, fields["token"], fields["new_password"], _client_ip(request))
    return _empty_response(request, clear_cookie=True)


@router.post("/auth/password/change", status_code=204)
async def change_password(request: Request, user: dict = Depends(require_mutation_user)):
    fields = await _json_fields(request, {"current_password": 128, "new_password": 128})
    await run_in_threadpool(_identity_call, identity.change_password, user["id"], request.cookies.get(SESSION_COOKIE), fields["current_password"], fields["new_password"], _client_ip(request))
    return _empty_response(request, clear_cookie=True)


@router.get("/auth/sessions")
def sessions(request: Request, user: dict = Depends(require_user)):
    result = _identity_call(identity.list_sessions, user["id"], request.cookies.get(SESSION_COOKIE))
    return JSONResponse({"sessions": result}, headers={"Cache-Control": "no-store"})


@router.delete("/auth/sessions/{session_id}", status_code=204)
def revoke_session_by_id(session_id: str, request: Request, user: dict = Depends(require_mutation_user)):
    current = _identity_call(identity.revoke_session_by_id, user["id"], request.cookies.get(SESSION_COOKIE), session_id)
    return _empty_response(request, clear_cookie=current)


@router.post("/auth/sessions/revoke-others", status_code=204)
def revoke_other_sessions(request: Request, user: dict = Depends(require_mutation_user)):
    _identity_call(identity.revoke_other_sessions, user["id"], request.cookies.get(SESSION_COOKIE))
    return _empty_response(request)
