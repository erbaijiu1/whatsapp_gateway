#!/usr/bin/env python3
"""Send a signed local WhatsApp webhook without contacting Meta."""

import argparse
import hashlib
import hmac
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request
import uuid


PROJECT_DIR = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="模拟 Meta WhatsApp 入站回调")
    parser.add_argument(
        "--content",
        default="我有100公斤芒果，从深圳发到香港观塘，有电梯，请问多少钱？",
        help="客户消息正文",
    )
    parser.add_argument("--phone", default="85290002024", help="模拟客户 WhatsApp 号码")
    parser.add_argument("--name", default="本地模拟客户", help="模拟客户名称")
    parser.add_argument("--account-code", default="local_dev", help="接收账号代码")
    parser.add_argument(
        "--message-id",
        default=f"wamid.local.{uuid.uuid4().hex}",
        help="Meta 消息 ID；复用同一 ID 可验证去重",
    )
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8011/whatsapp_gateway/api/webhook",
        help="本地 webhook 地址，默认通过网关的宿主机调试端口",
    )
    parser.add_argument(
        "--accounts-file",
        type=Path,
        default=PROJECT_DIR / "config" / "accounts.json",
        help="本地账号配置文件",
    )
    return parser.parse_args()


def load_account(accounts_file: Path, account_code: str) -> dict:
    accounts = json.loads(accounts_file.read_text())
    for account in accounts:
        if account.get("account_code") == account_code:
            return account
    raise ValueError(f"accounts.json 中不存在账号：{account_code}")


def build_payload(args: argparse.Namespace, account: dict) -> bytes:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {
                                "phone_number_id": account["phone_number_id"],
                                "display_phone_number": "local-dev",
                            },
                            "contacts": [
                                {
                                    "wa_id": args.phone,
                                    "profile": {"name": args.name},
                                }
                            ],
                            "messages": [
                                {
                                    "id": args.message_id,
                                    "from": args.phone,
                                    "type": "text",
                                    "text": {"body": args.content},
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def main() -> int:
    args = parse_args()
    try:
        account = load_account(args.accounts_file, args.account_code)
        body = build_payload(args, account)
        digest = hmac.new(account["app_secret"].encode(), body, hashlib.sha256).hexdigest()
        request = urllib.request.Request(
            args.url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": f"sha256={digest}",
            },
            method="POST",
        )
        # 本地请求不应受系统 HTTP_PROXY/HTTPS_PROXY 影响。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=15) as response:
            print(f"message_id={args.message_id}")
            print(f"status={response.status}")
            print(response.read().decode())
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError, urllib.error.URLError) as exc:
        print(f"模拟回调失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
