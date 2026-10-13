"""Unit tests for Cloud and Local Storage MCP integration."""
import base64
from pathlib import Path

import pytest

from integrations.mcp.azure_client import AzureBlobClient
from integrations.mcp.cloud_storage_server import CloudStorageMCPServer
from integrations.mcp.local_storage import LocalStorage, sanitize_filename
from integrations.mcp.s3_client import S3Client
from services.storage_service import mask_secret

pytestmark = pytest.mark.unit


def test_sanitize_filename():
    assert sanitize_filename("test file!@#.png") == "test_file___.png"
    assert sanitize_filename("../../../etc/passwd") == "passwd"
    assert sanitize_filename("normal.pdf") == "normal.pdf"


def test_mask_secret():
    assert mask_secret("") == ""
    assert mask_secret("12345") == "******"
    assert mask_secret("AKIAIOSFODNN7EXAMPLE") == "AKI...PLE"


def test_local_storage_roundtrip(tmp_path: Path):
    store = LocalStorage(root_dir=tmp_path)
    rel_path, stored_name = store.save_file("ws-1", "iss-1", "test.txt", b"hello world")
    assert "test.txt" in stored_name

    content = store.read_file(rel_path)
    assert content == b"hello world"

    deleted = store.delete_file(rel_path)
    assert deleted is True

    with pytest.raises(FileNotFoundError):
        store.read_file(rel_path)


def test_local_storage_traversal_protection(tmp_path: Path):
    store = LocalStorage(root_dir=tmp_path)
    with pytest.raises(ValueError, match="path traversal"):
        store.read_file("../../../secret.txt")


def test_s3_client_signature():
    s3 = S3Client(
        bucket="my-bucket",
        access_key_id="TESTKEY",
        secret_access_key="TESTSECRET",
        region="us-east-1",
    )
    url, host, path = s3._resolve_url_and_host("folder/image.png")
    assert "my-bucket.s3.us-east-1.amazonaws.com" in url
    assert host == "my-bucket.s3.us-east-1.amazonaws.com"
    assert path == "/folder/image.png"

    headers = s3._build_signed_headers("PUT", path, host, b"data", "image/png")
    assert "Authorization" in headers
    assert "AWS4-HMAC-SHA256" in headers["Authorization"]
    assert "Credential=TESTKEY" in headers["Authorization"]
    assert headers["Content-Type"] == "image/png"


def test_azure_client_signature():
    account_key = base64.b64encode(b"0" * 32).decode("utf-8")
    azure = AzureBlobClient(
        account_name="testaccount",
        account_key=account_key,
        container_name="testcontainer",
    )
    headers = azure._build_auth_headers(
        method="PUT",
        resource_path="/testcontainer/file.txt",
        content_length=5,
        content_type="text/plain",
    )
    assert "Authorization" in headers
    assert headers["Authorization"].startswith("SharedKey testaccount:")
    assert "x-ms-date" in headers
    assert headers["x-ms-version"] == "2020-10-02"


def test_mcp_server_local_fallback(tmp_path: Path):
    server = CloudStorageMCPServer(local_storage=LocalStorage(root_dir=tmp_path))

    tools = server.list_tools()
    tool_names = [t["name"] for t in tools]
    assert "storage_upload_file" in tool_names
    assert "storage_download_file" in tool_names
    assert "storage_test_connection" in tool_names

    # Upload with unconfigured cloud provider triggers local fallback
    b64_content = base64.b64encode(b"sample file").decode("utf-8")
    res = server.call_tool(
        "storage_upload_file",
        {
            "workspace_id": "ws-100",
            "issue_id": "iss-200",
            "filename": "sample.txt",
            "content_base64": b64_content,
            "provider": "aws_s3",
            "config": {
                "aws_bucket_name": "",  # Unconfigured, will fall back to local
                "fallback_to_local": True,
            },
        },
    )
    assert res["storage_provider"] == "local"
    assert res["fallback_used"] is True

    # Read back through MCP
    read_res = server.call_tool(
        "storage_download_file",
        {"storage_path": res["storage_path"], "provider": "local"},
    )
    assert read_res["content_bytes"] == b"sample file"
