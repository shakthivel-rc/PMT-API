"""Pydantic schemas for workspace storage settings and issue attachments."""
from typing import Any, Literal

from pydantic import BaseModel, Field


class UserRef(BaseModel):
    """A person, as much of them as a card, comment, or attachment needs."""

    id: str = Field(description="The account's id.")
    display_name: str = Field(description="`First Last`, for avatars and bylines.")

StorageProvider = Literal["local", "aws_s3", "azure_blob"]


class StorageSettings(BaseModel):
    """The workspace's object storage settings.

    Sensitive keys are masked on read.
    """

    provider: StorageProvider = Field(
        default="local", description="Active storage provider ('local', 'aws_s3', or 'azure_blob')."
    )
    aws_access_key_id: str = Field(
        default="", description="AWS Access Key ID (masked on read)."
    )
    aws_secret_access_key: str = Field(
        default="", description="AWS Secret Access Key (masked on read)."
    )
    aws_bucket_name: str = Field(default="", description="Target S3 Bucket Name.")
    aws_region: str = Field(default="us-east-1", description="AWS Region (e.g., us-east-1).")
    aws_endpoint_url: str | None = Field(
        default=None, description="Custom S3 endpoint URL (for MinIO, LocalStack, etc.)."
    )
    azure_account_name: str = Field(default="", description="Azure Storage Account Name.")
    azure_account_key: str = Field(
        default="", description="Azure Storage Account SharedKey (masked on read)."
    )
    azure_container_name: str = Field(default="", description="Target Azure Blob Container Name.")
    fallback_to_local: bool = Field(
        default=True,
        description=(
            "Automatically fall back to local disk storage if cloud credentials are unset "
            "or unreachable."
        ),
    )
    is_configured: bool = Field(
        default=False, description="Whether valid cloud credentials are fully configured."
    )


class StorageSettingsUpdate(BaseModel):
    """Payload to update workspace storage configuration."""

    provider: StorageProvider | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_bucket_name: str | None = None
    aws_region: str | None = None
    aws_endpoint_url: str | None = None
    azure_account_name: str | None = None
    azure_account_key: str | None = None
    azure_container_name: str | None = None
    fallback_to_local: bool | None = None


class StorageTestRequest(BaseModel):
    """Request payload to test cloud storage connection."""

    provider: StorageProvider = "aws_s3"
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_bucket_name: str | None = None
    aws_region: str | None = "us-east-1"
    aws_endpoint_url: str | None = None
    azure_account_name: str | None = None
    azure_account_key: str | None = None
    azure_container_name: str | None = None


class StorageTestResponse(BaseModel):
    """Result of a storage connectivity test."""

    success: bool
    message: str
    provider: str
    details: dict[str, Any] | None = None


class AttachmentSummary(BaseModel):
    """Attachment information returned to clients."""

    id: str
    workspace_id: str
    issue_id: str
    comment_id: str | None = None
    uploader: UserRef
    filename: str
    file_size: int
    content_type: str
    storage_provider: str
    is_image: bool
    created_at: str
    download_url: str


class AttachmentUploadBase64(BaseModel):
    """JSON payload for base64 file upload."""

    filename: str
    content_type: str = "application/octet-stream"
    content_base64: str
    comment_id: str | None = None
