"""Model Context Protocol (MCP) server for Cloud & Local Storage.

Exposes MCP tools for file uploads, downloads, deletions, and connectivity
tests across AWS S3, Azure Blob Storage, and local filesystem fallback.
"""
import base64
import logging
import uuid
from typing import Any

from integrations.mcp.azure_client import AzureBlobClient
from integrations.mcp.local_storage import LocalStorage, sanitize_filename
from integrations.mcp.s3_client import S3Client

logger = logging.getLogger(__name__)


class CloudStorageMCPServer:
    """MCP server providing unified object storage operations with fallback to local."""

    def __init__(self, local_storage: LocalStorage | None = None) -> None:
        self.local_storage = local_storage or LocalStorage()

    def list_tools(self) -> list[dict[str, Any]]:
        """Return MCP tool declarations following the Model Context Protocol specification."""
        return [
            {
                "name": "storage_upload_file",
                "description": (
                    "Upload a file or image to cloud storage (AWS S3 / Azure Blob) "
                    "with local fallback."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "issue_id": {"type": "string"},
                        "filename": {"type": "string"},
                        "content_base64": {"type": "string"},
                        "content_type": {"type": "string"},
                        "provider": {
                            "type": "string",
                            "enum": ["auto", "local", "aws_s3", "azure_blob"],
                        },
                        "config": {"type": "object"},
                    },
                    "required": ["filename", "content_base64"],
                },
            },
            {
                "name": "storage_download_file",
                "description": "Download a file from cloud storage or local disk.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "storage_path": {"type": "string"},
                        "provider": {"type": "string", "enum": ["local", "aws_s3", "azure_blob"]},
                        "config": {"type": "object"},
                    },
                    "required": ["storage_path", "provider"],
                },
            },
            {
                "name": "storage_delete_file",
                "description": "Delete a file from cloud storage or local disk.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "storage_path": {"type": "string"},
                        "provider": {"type": "string", "enum": ["local", "aws_s3", "azure_blob"]},
                        "config": {"type": "object"},
                    },
                    "required": ["storage_path", "provider"],
                },
            },
            {
                "name": "storage_test_connection",
                "description": (
                    "Test connectivity and credentials for AWS S3 or Azure Blob Storage."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "provider": {"type": "string", "enum": ["aws_s3", "azure_blob"]},
                        "config": {"type": "object"},
                    },
                    "required": ["provider", "config"],
                },
            },
        ]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Dispatch an MCP tool call by name."""
        if name == "storage_upload_file":
            return self.upload_file(arguments)
        if name == "storage_download_file":
            return self.download_file(arguments)
        if name == "storage_delete_file":
            return self.delete_file(arguments)
        if name == "storage_test_connection":
            return self.test_connection(arguments)
        raise ValueError(f"Unknown MCP storage tool: '{name}'")

    def upload_file(self, args: dict[str, Any]) -> dict[str, Any]:
        workspace_id = args.get("workspace_id", "default")
        issue_id = args.get("issue_id", "general")
        filename = args.get("filename", "upload.bin")
        content_base64 = args["content_base64"]
        content_type = args.get("content_type", "application/octet-stream")
        provider = args.get("provider", "local")
        config = args.get("config") or {}
        fallback_to_local = config.get("fallback_to_local", True)

        try:
            content = base64.b64decode(content_base64)
        except Exception as exc:
            raise ValueError(f"Invalid base64 payload: {exc}") from exc

        file_size = len(content)
        safe_filename = sanitize_filename(filename)
        stored_filename = f"{uuid.uuid4().hex[:12]}_{safe_filename}"
        cloud_key = f"{workspace_id}/{issue_id}/{stored_filename}"

        # 1. AWS S3 Upload
        if provider == "aws_s3":
            bucket = config.get("aws_bucket_name")
            access_key = config.get("aws_access_key_id")
            secret_key = config.get("aws_secret_access_key")
            region = config.get("aws_region", "us-east-1")
            endpoint = config.get("aws_endpoint_url")

            if bucket and access_key and secret_key:
                try:
                    s3 = S3Client(
                        bucket=bucket,
                        access_key_id=access_key,
                        secret_access_key=secret_key,
                        region=region,
                        endpoint_url=endpoint,
                    )
                    res = s3.upload_file(cloud_key, content, content_type)
                    return {
                        "storage_provider": "aws_s3",
                        "storage_path": cloud_key,
                        "stored_filename": stored_filename,
                        "file_size": file_size,
                        "fallback_used": False,
                        "cloud_url": res["url"],
                    }
                except Exception as exc:
                    logger.warning(
                        "AWS S3 upload failed for '%s', falling back to local: %s", filename, exc
                    )
                    if not fallback_to_local:
                        raise

        # 2. Azure Blob Upload
        elif provider == "azure_blob":
            account = config.get("azure_account_name")
            account_key = config.get("azure_account_key")
            container = config.get("azure_container_name")

            if account and account_key and container:
                try:
                    azure = AzureBlobClient(
                        account_name=account,
                        account_key=account_key,
                        container_name=container,
                    )
                    res = azure.upload_blob(cloud_key, content, content_type)
                    return {
                        "storage_provider": "azure_blob",
                        "storage_path": cloud_key,
                        "stored_filename": stored_filename,
                        "file_size": file_size,
                        "fallback_used": False,
                        "cloud_url": res["url"],
                    }
                except Exception as exc:
                    logger.warning(
                        "Azure Blob upload failed for '%s', falling back to local: %s",
                        filename,
                        exc,
                    )
                    if not fallback_to_local:
                        raise

        # 3. Local filesystem storage (default or fallback)
        local_rel_path, local_stored_name = self.local_storage.save_file(
            workspace_id=workspace_id,
            issue_id=issue_id,
            filename=filename,
            content=content,
        )
        return {
            "storage_provider": "local",
            "storage_path": local_rel_path,
            "stored_filename": local_stored_name,
            "file_size": file_size,
            "fallback_used": (provider in ("aws_s3", "azure_blob")),
        }

    def download_file(self, args: dict[str, Any]) -> dict[str, Any]:
        storage_path = args["storage_path"]
        provider = args.get("provider", "local")
        config = args.get("config") or {}

        if provider == "aws_s3":
            bucket = config.get("aws_bucket_name")
            access_key = config.get("aws_access_key_id")
            secret_key = config.get("aws_secret_access_key")
            region = config.get("aws_region", "us-east-1")
            endpoint = config.get("aws_endpoint_url")
            s3 = S3Client(bucket, access_key, secret_key, region, endpoint)
            content = s3.download_file(storage_path)
        elif provider == "azure_blob":
            account = config.get("azure_account_name")
            account_key = config.get("azure_account_key")
            container = config.get("azure_container_name")
            azure = AzureBlobClient(account, account_key, container)
            content = azure.download_blob(storage_path)
        else:
            content = self.local_storage.read_file(storage_path)

        return {
            "content_bytes": content,
            "content_base64": base64.b64encode(content).decode("utf-8"),
            "size": len(content),
        }

    def delete_file(self, args: dict[str, Any]) -> dict[str, Any]:
        storage_path = args["storage_path"]
        provider = args.get("provider", "local")
        config = args.get("config") or {}

        success = False
        if provider == "aws_s3":
            bucket = config.get("aws_bucket_name")
            access_key = config.get("aws_access_key_id")
            secret_key = config.get("aws_secret_access_key")
            region = config.get("aws_region", "us-east-1")
            endpoint = config.get("aws_endpoint_url")
            if bucket and access_key and secret_key:
                s3 = S3Client(bucket, access_key, secret_key, region, endpoint)
                success = s3.delete_file(storage_path)
        elif provider == "azure_blob":
            account = config.get("azure_account_name")
            account_key = config.get("azure_account_key")
            container = config.get("azure_container_name")
            if account and account_key and container:
                azure = AzureBlobClient(account, account_key, container)
                success = azure.delete_blob(storage_path)
        else:
            success = self.local_storage.delete_file(storage_path)

        return {"deleted": success, "storage_path": storage_path}

    def test_connection(self, args: dict[str, Any]) -> dict[str, Any]:
        provider = args.get("provider", "aws_s3")
        config = args.get("config") or {}

        if provider == "aws_s3":
            bucket = config.get("aws_bucket_name", "")
            access_key = config.get("aws_access_key_id", "")
            secret_key = config.get("aws_secret_access_key", "")
            region = config.get("aws_region", "us-east-1")
            endpoint = config.get("aws_endpoint_url")

            if not bucket:
                return {
                    "success": False,
                    "message": "AWS Bucket Name is required.",
                    "provider": "aws_s3",
                }
            if not access_key:
                return {
                    "success": False,
                    "message": "AWS Access Key ID is required.",
                    "provider": "aws_s3",
                }
            if not secret_key:
                return {
                    "success": False,
                    "message": "AWS Secret Access Key is required.",
                    "provider": "aws_s3",
                }

            s3 = S3Client(bucket, access_key, secret_key, region, endpoint)
            ok, msg = s3.test_connection()
            return {"success": ok, "message": msg, "provider": "aws_s3"}

        if provider == "azure_blob":
            account = config.get("azure_account_name", "")
            account_key = config.get("azure_account_key", "")
            container = config.get("azure_container_name", "")

            if not account:
                return {
                    "success": False,
                    "message": "Azure Storage Account Name is required.",
                    "provider": "azure_blob",
                }
            if not account_key:
                return {
                    "success": False,
                    "message": "Azure Storage Account Key is required.",
                    "provider": "azure_blob",
                }
            if not container:
                return {
                    "success": False,
                    "message": "Azure Container Name is required.",
                    "provider": "azure_blob",
                }

            azure = AzureBlobClient(account, account_key, container)
            ok, msg = azure.test_connection()
            return {"success": ok, "message": msg, "provider": "azure_blob"}

        return {"success": True, "message": "Local storage is available.", "provider": "local"}


# Singleton instance
default_mcp_storage_server = CloudStorageMCPServer()
