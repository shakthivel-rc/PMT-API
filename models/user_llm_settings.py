"""Per-user LLM configuration model: provider, model, API key, base URL."""
import uuid
from datetime import UTC, datetime

from sqlalchemy import CHAR, Column, DateTime, ForeignKey, Index, Integer, String, Text

from db.session import Base
from utils.token import encrypt_string, decrypt_string, encode_to_base64, decode_base64


def _now() -> datetime:
    return datetime.now(UTC)


class UserLLMSettings(Base):
    __tablename__ = "user_llm_settings"
    __table_args__ = (Index("ix_user_llm_settings_user", "user_id"),)

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(CHAR(36), ForeignKey("users.id"), nullable=False, unique=True)
    provider = Column(String(50), nullable=False, default="openai")  # openai, anthropic, google, openai_compatible
    model = Column(String(100), nullable=False, default="gpt-4o")
    encrypted_api_key = Column(Text, nullable=True)
    base_url = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=_now, nullable=False)
    updated_at = Column(DateTime, default=_now, onupdate=_now, nullable=False)

    def set_api_key(self, api_key: str | None) -> None:
        if not api_key:
            self.encrypted_api_key = None
            return
        enc_bytes = encrypt_string(api_key)
        self.encrypted_api_key = encode_to_base64(enc_bytes)

    def get_api_key(self) -> str | None:
        if not self.encrypted_api_key:
            return None
        try:
            raw_bytes = decode_base64(self.encrypted_api_key).encode("utf-8") if isinstance(self.encrypted_api_key, str) else self.encrypted_api_key
            # Handle decode_base64 return type
            import base64
            enc_bytes = base64.b64decode(self.encrypted_api_key)
            return decrypt_string(enc_bytes)
        except Exception:
            return None
