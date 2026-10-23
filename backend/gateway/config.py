import json
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WhatsAppAccount(BaseModel):
    account_code: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,60}$")
    phone_number_id: str = Field(pattern=r"^[0-9]+$")
    access_token: SecretStr = Field(min_length=1)
    verify_token: SecretStr = Field(min_length=1)
    app_secret: SecretStr = Field(min_length=1)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    agent_base_url: str = "http://logistics-ai-agent:8000/logistics_ai_agent/api"
    agent_ingress_token: SecretStr = Field(min_length=32)
    gateway_send_token: SecretStr = Field(min_length=32)
    accounts_file: Path = Path("config/accounts.json")
    data_dir: Path = Path("data")
    graph_api_version: str = Field(default="v23.0", pattern=r"^v[0-9]+\.[0-9]+$")
    request_timeout_seconds: float = Field(default=10, gt=0, le=30)
    max_webhook_bytes: int = Field(default=1048576, ge=1024)

    @model_validator(mode="after")
    def validate_agent_url(self):
        if not self.agent_base_url.startswith(("http://", "https://")):
            raise ValueError("agent_base_url must be an HTTP URL")
        return self

    def load_accounts(self) -> list[WhatsAppAccount]:
        accounts = [WhatsAppAccount.model_validate(row) for row in json.loads(self.accounts_file.read_text())]
        if not accounts:
            raise ValueError("Configure at least one WhatsApp account")
        for field in ("account_code", "phone_number_id"):
            if len({getattr(account, field) for account in accounts}) != len(accounts):
                raise ValueError(f"Duplicate {field} in accounts file")
        return accounts
