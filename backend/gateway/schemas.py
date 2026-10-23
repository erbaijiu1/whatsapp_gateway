from pydantic import BaseModel, Field


class SendTextRequest(BaseModel):
    message_id: str = Field(min_length=1, max_length=160)
    account_code: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,60}$")
    recipient: str = Field(pattern=r"^[0-9]{5,20}$")
    content: str = Field(min_length=1, max_length=4096)
