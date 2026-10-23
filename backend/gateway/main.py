from contextlib import asynccontextmanager
import hmac
import json

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from gateway.config import Settings
from gateway.delivery_store import DeliveryStore
from gateway.logger import configure_logging
from gateway.schemas import SendTextRequest
from gateway.service import WhatsAppGateway
from gateway.webhooks import parse_inbound_messages, verify_signature

PREFIX = "/whatsapp_gateway/api"


def create_app(config: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    settings = config or Settings()
    accounts = settings.load_accounts()
    deliveries = DeliveryStore(settings.data_dir / "deliveries.sqlite3")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging(settings.data_dir)
        deliveries.initialize()
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds, transport=transport) as client:
            app.state.gateway = WhatsAppGateway(settings, accounts, client, deliveries)
            yield

    app = FastAPI(title="WhatsApp Gateway", docs_url=f"{PREFIX}/docs", openapi_url=f"{PREFIX}/openapi.json", lifespan=lifespan)
    router = APIRouter(prefix=PREFIX)

    async def require_service_token(authorization: str = Header(default="")) -> None:
        if not hmac.compare_digest(authorization.encode(), f"Bearer {settings.gateway_send_token.get_secret_value()}".encode()):
            raise HTTPException(401, "Invalid service token")

    @router.get("/health")
    async def health():
        return {"status": "ok", "service": "whatsapp_gateway"}

    @router.get("/webhook", response_class=PlainTextResponse)
    @router.get("/webhook/{account_code}", response_class=PlainTextResponse)
    async def verify_webhook(
        account_code: str | None = None,
        mode: str = Query(default="", alias="hub.mode"),
        token: str = Query(default="", alias="hub.verify_token"),
        challenge: str = Query(default="", alias="hub.challenge"),
    ):
        candidates = [account for account in accounts if account_code is None or account.account_code == account_code]
        if mode != "subscribe" or not challenge or not any(hmac.compare_digest(token.encode(), account.verify_token.get_secret_value().encode()) for account in candidates):
            raise HTTPException(403, "Webhook verification failed")
        return challenge

    @router.post("/webhook")
    @router.post("/webhook/{account_code}")
    async def receive_webhook(request: Request, account_code: str | None = None):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > settings.max_webhook_bytes:
                raise HTTPException(413, "Webhook body too large")
        signature = request.headers.get("x-hub-signature-256", "")
        # 空事件同样必须验签；包含具体账号的事件会逐个再次校验其绑定的 app secret。
        candidates = [account for account in accounts if account_code is None or account.account_code == account_code]
        verified = False
        for account in candidates:
            try:
                verify_signature(bytes(body), signature, account)
                verified = True
                break
            except HTTPException:
                continue
        if not verified:
            raise HTTPException(403, "Invalid webhook signature")
        try:
            payload = json.loads(body)
            messages = parse_inbound_messages(payload, accounts, bytes(body), signature, account_code)
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            raise HTTPException(400, "Invalid webhook payload") from exc
        await request.app.state.gateway.forward_to_agent(messages)
        return {"status": "accepted", "message_count": len(messages)}

    @router.post("/internal/messages", dependencies=[Depends(require_service_token)])
    async def send_text(message: SendTextRequest, request: Request):
        provider_id = await request.app.state.gateway.send_text(message)
        return {"status": "sent", "message_id": provider_id}

    app.include_router(router)
    return app
