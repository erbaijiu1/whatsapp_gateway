import hashlib
import hmac
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.config import Settings
from gateway.main import create_app


@pytest.fixture
def config(tmp_path):
    accounts = tmp_path / "accounts.json"
    accounts.write_text(json.dumps([
        {"account_code": code, "phone_number_id": phone,
         "access_token": f"test-access-{code}", "verify_token": f"test-verify-{code}", "app_secret": f"test-secret-{code}"}
        for code, phone in [("sales_a", "111"), ("sales_b", "222")]
    ]))
    return Settings(_env_file=None, accounts_file=accounts, data_dir=tmp_path / "data",
                    agent_ingress_token="i" * 32, gateway_send_token="s" * 32,
                    agent_base_url="http://agent/logistics_ai_agent/api")


def event(phone="111", messages=None):
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": phone},
        "messages": messages if messages is not None else [
            {"id": "wamid.inbound", "from": "85212345678", "type": "text", "text": {"body": "mango 100kg"}}
        ],
    }}]}]}


def signed(payload, secret="test-secret-sales_a"):
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"content": body, "headers": {"x-hub-signature-256": signature, "content-type": "application/json"}}


def send_request():
    return {"message_id": "local-1", "account_code": "sales_b", "recipient": "85212345678", "content": "quote 351"}


AUTH = {"Authorization": "Bearer " + "s" * 32}


def test_verification_rejects_wrong_account_and_empty_token(config):
    with TestClient(create_app(config)) as client:
        url = "/whatsapp_gateway/api/webhook/sales_a"
        query = {"hub.mode": "subscribe", "hub.verify_token": "test-verify-sales_a", "hub.challenge": "12345"}
        assert client.get(url, params=query).text == "12345"
        assert client.get(url.replace("sales_a", "sales_b"), params=query).status_code == 403
        query["hub.verify_token"] = ""
        assert client.get(url, params=query).status_code == 403


def test_valid_webhook_preserves_customer_account_and_id_and_retries(config):
    received = []
    def upstream(request):
        assert request.headers["Authorization"] == "Bearer " + "i" * 32
        assert request.url.path == "/logistics_ai_agent/api/internal/whatsapp/messages"
        received.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "buffered" if len(received) == 1 else "duplicate"})
    with TestClient(create_app(config, httpx.MockTransport(upstream))) as client:
        for _ in range(2):
            assert client.post("/whatsapp_gateway/api/webhook", **signed(event())).status_code == 200
    assert received[0] == received[1]
    assert received[0]["account_code"] == "sales_a"
    assert received[0]["phone_number_id"] == "111"
    assert received[0]["whatsapp_user_id"] == "85212345678"
    assert received[0]["message_id"] == "wamid.inbound"


def test_signature_and_receiving_account_are_checked_before_forwarding(config):
    def no_network(request):
        pytest.fail("Invalid webhook must not reach upstream")
    with TestClient(create_app(config, httpx.MockTransport(no_network))) as client:
        assert client.post("/whatsapp_gateway/api/webhook", json=event()).status_code == 403
        assert client.post("/whatsapp_gateway/api/webhook", **signed(event(), "wrong")).status_code == 403
        assert client.post("/whatsapp_gateway/api/webhook/sales_b", **signed(event())).status_code == 403
        assert client.post("/whatsapp_gateway/api/webhook", **signed(event("222"))).status_code == 403
        assert client.post("/whatsapp_gateway/api/webhook", **signed(event("999"))).status_code == 403


def test_failed_ingress_is_not_acknowledged_and_status_events_do_not_trigger_ai(config):
    with TestClient(create_app(config, httpx.MockTransport(lambda request: httpx.Response(503)))) as client:
        assert client.post("/whatsapp_gateway/api/webhook", **signed(event())).status_code == 503
        assert client.post("/whatsapp_gateway/api/webhook", **signed(event(messages=[]))).json()["message_count"] == 0


def test_internal_send_auth_account_routing_and_persistent_deduplication(config):
    sent = []
    def meta(request):
        sent.append(request)
        assert request.url.path.endswith("/222/messages")
        assert request.headers["Authorization"] == "Bearer test-access-sales_b"
        assert json.loads(request.content)["to"] == "85212345678"
        return httpx.Response(200, json={"messages": [{"id": "wamid.sent"}]})
    path = "/whatsapp_gateway/api/internal/messages"
    with TestClient(create_app(config, httpx.MockTransport(meta))) as client:
        assert client.post(path, json=send_request()).status_code == 401
        assert client.post(path, headers=AUTH, json=send_request()).json()["message_id"] == "wamid.sent"
        changed = {**send_request(), "recipient": "85299999999"}
        assert client.post(path, headers=AUTH, json=changed).status_code == 409
    # 重启仍使用相同的发送凭证，不会重复调用 Meta。
    with TestClient(create_app(config, httpx.MockTransport(meta))) as client:
        assert client.post(path, headers=AUTH, json=send_request()).json()["message_id"] == "wamid.sent"
        assert client.post(path, headers=AUTH, json={**send_request(), "account_code": "unknown"}).status_code == 404
    assert len(sent) == 1


def test_meta_timeout_is_not_automatically_resent(config):
    attempts = []
    def timeout(request):
        attempts.append(request)
        raise httpx.ReadTimeout("timeout")
    path = "/whatsapp_gateway/api/internal/messages"
    with TestClient(create_app(config, httpx.MockTransport(timeout))) as client:
        assert client.post(path, headers=AUTH, json=send_request()).status_code == 502
        assert client.post(path, headers=AUTH, json=send_request()).status_code == 409
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_concurrent_outbound_requests_only_submit_once(config):
    import asyncio
    calls = []
    async def meta(request):
        calls.append(request)
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"messages": [{"id": "wamid.concurrent"}]})
    app = create_app(config, httpx.MockTransport(meta))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            responses = await asyncio.gather(*[
                client.post("/whatsapp_gateway/api/internal/messages", headers=AUTH, json=send_request()) for _ in range(2)
            ])
    assert 200 in [response.status_code for response in responses]
    assert len(calls) == 1
