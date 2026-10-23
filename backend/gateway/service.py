import logging

import httpx
from fastapi import HTTPException

from gateway.config import Settings, WhatsAppAccount
from gateway.delivery_store import DeliveryStore
from gateway.schemas import SendTextRequest

logger = logging.getLogger("whatsapp_gateway")


class WhatsAppGateway:
    def __init__(self, settings: Settings, accounts: list[WhatsAppAccount], client: httpx.AsyncClient, deliveries: DeliveryStore):
        self.settings = settings
        self.accounts = {account.account_code: account for account in accounts}
        self.client = client
        self.deliveries = deliveries

    async def forward_to_agent(self, messages: list[dict]) -> None:
        for message in messages:
            try:
                response = await self.client.post(
                    f"{self.settings.agent_base_url.rstrip('/')}/internal/whatsapp/messages",
                    headers={"Authorization": f"Bearer {self.settings.agent_ingress_token.get_secret_value()}"},
                    json=message,
                )
                response.raise_for_status()
                if response.json().get("status") not in {"buffered", "duplicate", "manual_mode"}:
                    raise ValueError("Agent did not confirm message persistence")
            except (httpx.HTTPError, ValueError, AttributeError) as exc:
                logger.warning("Agent ingress failed message_id=%s error=%s", message["message_id"], type(exc).__name__)
                # 不先应答成功：Meta 重试后，报价服务按原消息 ID 去重。
                raise HTTPException(503, "Agent unavailable; retry webhook") from exc
            logger.info("Forwarded inbound message_id=%s account=%s", message["message_id"], message["account_code"])

    async def send_text(self, request: SendTextRequest) -> str:
        account = self.accounts.get(request.account_code)
        if account is None:
            raise HTTPException(404, "Unknown sending account")
        previous_id = await self.deliveries.claim(request)
        if previous_id:
            return previous_id
        try:
            response = await self.client.post(
                f"https://graph.facebook.com/{self.settings.graph_api_version}/{account.phone_number_id}/messages",
                headers={"Authorization": f"Bearer {account.access_token.get_secret_value()}"},
                json={"messaging_product": "whatsapp", "to": request.recipient,
                      "type": "text", "text": {"body": request.content}},
            )
            response.raise_for_status()
            provider_id = response.json()["messages"][0]["id"]
            if not isinstance(provider_id, str) or not provider_id:
                raise ValueError("Missing provider message ID")
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            await self.deliveries.finish(request.message_id, "UNCERTAIN")
            logger.warning("Outbound not confirmed message_id=%s error=%s", request.message_id, type(exc).__name__)
            raise HTTPException(502, "WhatsApp delivery not confirmed; inspect before retry") from exc
        await self.deliveries.finish(request.message_id, "SENT", provider_id)
        logger.info("Submitted outbound message_id=%s provider_id=%s account=%s", request.message_id, provider_id, request.account_code)
        return provider_id
