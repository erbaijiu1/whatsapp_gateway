import hashlib
import hmac
import logging

from fastapi import HTTPException

from gateway.config import WhatsAppAccount

logger = logging.getLogger("whatsapp_gateway")


def verify_signature(body: bytes, signature: str, account: WhatsAppAccount) -> None:
    expected = "sha256=" + hmac.new(account.app_secret.get_secret_value().encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature.encode(), expected.encode()):
        raise HTTPException(403, "Invalid webhook signature")


def parse_inbound_messages(payload: dict, accounts: list[WhatsAppAccount], body: bytes, signature: str, account_code: str | None) -> list[dict]:
    if payload.get("object") != "whatsapp_business_account":
        raise HTTPException(400, "Unsupported webhook object")
    accounts_by_phone = {account.phone_number_id: account for account in accounts}
    messages = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            metadata = value.get("metadata", {})
            account = accounts_by_phone.get(metadata.get("phone_number_id"))
            if account is None or (account_code is not None and account.account_code != account_code):
                raise HTTPException(403, "Unknown or mismatched receiving account")
            verify_signature(body, signature, account)
            contacts = {contact.get("wa_id"): contact for contact in value.get("contacts", [])}
            for message in value.get("messages", []):
                text = message_text(message)
                if text is None:
                    logger.info("Unsupported inbound type=%s id=%s", message.get("type"), message.get("id"))
                    continue
                if not message.get("id") or not message.get("from") or not text.strip():
                    raise HTTPException(400, "Inbound text message is missing required fields")
                messages.append({
                    "message_id": message["id"], "whatsapp_user_id": message["from"],
                    "phone": message["from"], "account_code": account.account_code,
                    "phone_number_id": account.phone_number_id,
                    "display_phone_number": metadata.get("display_phone_number"),
                    "whatsapp_name": contacts.get(message["from"], {}).get("profile", {}).get("name"),
                    "message_type": "text", "content": text,
                    "raw_data": {"source": "WHATSAPP_GATEWAY", "provider_message": message},
                })
    return messages


def message_text(message: dict) -> str | None:
    if message.get("type") == "text":
        return message.get("text", {}).get("body")
    if message.get("type") == "button":
        return message.get("button", {}).get("text")
    if message.get("type") == "interactive":
        interactive = message.get("interactive", {})
        choice = interactive.get("button_reply") or interactive.get("list_reply") or {}
        return choice.get("title")
    return None
