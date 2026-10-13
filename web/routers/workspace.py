"""Authenticated customer workspace; independent of the legacy control APIs."""
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from services import workspace_accounts as accounts
from services import workspace_observer, workspace_performance, workspace_usage
from services.identity import get_entitlements
from services.market_quotes import QuoteService
from services.market_types import Market, get_market_spec, list_instruments, market_session
from web.routers.identity import require_mutation_user, require_user, require_verified_trading_user, assert_verified_trading_user


router = APIRouter()


def get_quotes():
    return QuoteService()


class StrictBody(BaseModel):
    class Config:
        extra = "forbid"


class AccountBody(StrictBody):
    market: Literal["CN", "US", "CRYPTO"]
    name: Optional[str] = Field(default=None, max_length=60)


class OrderBody(StrictBody):
    instrument_id: str = Field(min_length=4, max_length=32)
    side: Literal["buy", "sell"]
    quantity: str = Field(min_length=1, max_length=32)
    request_id: str = Field(min_length=8, max_length=120)


class StrategyBody(StrictBody):
    enabled: bool
    instrument_id: str = Field(min_length=4, max_length=32)
    max_allocation: str = Field(default="0.20", max_length=12)


@router.get("/overview")
def overview(user=Depends(require_user)):
    return {"user": {k: user[k] for k in ("id", "email", "plan", "email_verified")}, "accounts": accounts.list_accounts(user["id"]), "limits": get_entitlements(user["plan"]), "billing": {"enabled": False, "plan": user["plan"]}, "mode": "paper"}


@router.get("/billing")
def billing(user=Depends(require_user)):
    return workspace_usage.get_usage(user["id"])


@router.get("/markets")
def markets(user=Depends(require_user)):
    result = []
    now = datetime.now(timezone.utc)
    for market in Market:
        item = get_market_spec(market).to_dict()
        item.update(id=market.value, instruments=[i.to_dict() for i in list_instruments(market)], session=market_session(market, now).to_dict(), strategy_available=market != Market.CN)
        result.append(item)
    return {"markets": result, "mode": "paper"}


@router.post("/accounts", status_code=201)
def create_account(body: AccountBody, user=Depends(require_verified_trading_user)):
    return accounts.create_account(user["id"], body.market, body.name, get_entitlements(user["plan"]))


@router.get("/accounts/{account_id}")
def account(account_id: str, user=Depends(require_user), quotes=Depends(get_quotes)):
    return accounts.get_account(user["id"], account_id, quotes=quotes)


@router.get("/accounts/{account_id}/performance")
def performance(account_id: str, limit: int = Query(default=1000, ge=1, le=2000), offset: int = Query(default=0, ge=0, le=1000000), user=Depends(require_user)):
    result = workspace_performance.get_performance(user["id"], account_id, limit=limit, offset=offset)
    result["observer"] = workspace_observer.status()
    return result


@router.get("/accounts/{account_id}/orders.csv")
def export_orders(account_id: str, user=Depends(require_user)):
    payload = workspace_performance.orders_csv(user["id"], account_id)
    return Response(payload, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="alphapilot-orders.csv"', "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/accounts/{account_id}/snapshots.csv")
def export_snapshots(account_id: str, user=Depends(require_user)):
    payload = workspace_performance.snapshots_csv(user["id"], account_id)
    return Response(payload, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="alphapilot-snapshots.csv"', "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/accounts/{account_id}/strategy/versions")
def strategy_versions(account_id: str, user=Depends(require_user)):
    return accounts.strategy_history(user["id"], account_id)


@router.get("/quote")
def quote(instrument_id: str, user=Depends(require_user), quotes=Depends(get_quotes)):
    instrument = accounts.instrument_from_id(instrument_id)
    result = quotes.get_quote(instrument.market, instrument.symbol)
    data = result.to_dict()
    session = market_session(instrument.market)
    data.update(instrument_id=instrument.instrument_id, symbol=instrument.symbol, name=instrument.name, currency=instrument.currency, provider=result.source, tradable=session.is_open, reason=session.reason, session=session.to_dict())
    return data


@router.post("/accounts/{account_id}/orders", status_code=201)
def order(account_id: str, body: OrderBody, user=Depends(require_verified_trading_user), quotes=Depends(get_quotes)):
    result = accounts.place_order(user["id"], account_id, body.instrument_id, body.side, body.quantity, body.request_id, quotes)
    result["account"] = accounts.get_account(user["id"], account_id)["account"]
    return result


@router.post("/accounts/{account_id}/strategy")
def strategy(account_id: str, body: StrategyBody, user=Depends(require_mutation_user), quotes=Depends(get_quotes)):
    if body.enabled:
        assert_verified_trading_user(user)
    return accounts.configure_strategy(user["id"], account_id, body.enabled, body.instrument_id, body.max_allocation, quotes)
