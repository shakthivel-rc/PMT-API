"""Integration tests for storage settings and issue attachments API."""
import base64

import pytest

from models.issue import IssueEvent
from tests import pm_fixtures as pm

pytestmark = pytest.mark.integration


class TestStorageSettings:
    def test_get_and_update_storage_settings(self, client, db):
        headers, workspace_id, _, _ = pm.setup_board(client, db)

        # 1. Default settings
        res = client.get(f"/workspaces/{workspace_id}/settings/storage", headers=headers)
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["provider"] == "local"
        assert data["fallback_to_local"] is True

        # 2. Update to AWS S3
        patch_payload = {
            "provider": "aws_s3",
            "aws_bucket_name": "production-bucket",
            "aws_access_key_id": "AKIA1234567890EXAMPLE",
            "aws_secret_access_key": "v/SecretKeyWithManyCharacters123",
            "aws_region": "us-west-2",
        }
        update_res = client.patch(
            f"/workspaces/{workspace_id}/settings/storage",
            json=patch_payload,
            headers=headers,
        )
        assert update_res.status_code == 200
        saved = update_res.json()["data"]
        assert saved["provider"] == "aws_s3"
        assert saved["aws_bucket_name"] == "production-bucket"
        assert saved["aws_region"] == "us-west-2"
        # Secret is masked
        assert saved["aws_secret_access_key"].startswith("v/S...")
        assert saved["is_configured"] is True

        # 3. Test connection endpoint
        test_res = client.post(
            f"/workspaces/{workspace_id}/settings/storage/test",
            json={"provider": "local"},
            headers=headers,
        )
        assert test_res.status_code == 200
        assert test_res.json()["data"]["success"] is True


class TestIssueAttachments:
    def test_upload_download_and_delete_attachment(self, client, db):
        headers, workspace_id, _, board_list = pm.setup_board(client, db)
        issue = pm.issue(client, headers, workspace_id, board_list["id"], title="Test Issue")
        issue_id = issue["id"]

        # 1. Upload via base64 JSON
        content_bytes = b"Hello, Antigravity attachment!"
        b64_content = base64.b64encode(content_bytes).decode("utf-8")
        upload_res = client.post(
            f"/workspaces/{workspace_id}/issues/{issue_id}/attachments",
            json={
                "filename": "hello.txt",
                "content_type": "text/plain",
                "content_base64": b64_content,
            },
            headers=headers,
        )
        assert upload_res.status_code == 201
        att = upload_res.json()["data"]
        attachment_id = att["id"]
        assert att["filename"] == "hello.txt"
        assert att["storage_provider"] == "local"
        assert att["file_size"] == len(content_bytes)

        # 2. List attachments
        list_res = client.get(
            f"/workspaces/{workspace_id}/issues/{issue_id}/attachments",
            headers=headers,
        )
        assert list_res.status_code == 200
        items = list_res.json()["data"]
        assert len(items) == 1
        assert items[0]["id"] == attachment_id

        # 3. Download attachment
        dl_res = client.get(
            f"/workspaces/{workspace_id}/issues/{issue_id}/attachments/{attachment_id}/download",
            headers=headers,
        )
        assert dl_res.status_code == 200
        assert dl_res.content == content_bytes
        assert dl_res.headers["content-type"].startswith("text/plain")

        # 4. Check issue events
        events = (
            db.query(IssueEvent)
            .filter(
                IssueEvent.issue_id == issue_id,
                IssueEvent.event == "attachment_added",
            )
            .all()
        )
        assert len(events) == 1
        assert events[0].to_value == "hello.txt"

        # 5. Delete attachment
        del_res = client.delete(
            f"/workspaces/{workspace_id}/issues/{issue_id}/attachments/{attachment_id}",
            headers=headers,
        )
        assert del_res.status_code == 200
        assert del_res.json()["data"]["deleted"] is True

        # 6. Verify deleted
        list_after = client.get(
            f"/workspaces/{workspace_id}/issues/{issue_id}/attachments",
            headers=headers,
        )
        assert len(list_after.json()["data"]) == 0

    def test_multipart_upload_attachment(self, client, db):
        headers, workspace_id, _, board_list = pm.setup_board(client, db)
        issue = pm.issue(client, headers, workspace_id, board_list["id"], title="Image Issue")
        issue_id = issue["id"]

        boundary = "----TestBoundary987654321"
        file_data = b"\x89PNG\r\n\x1a\nfake_image_bytes"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="screenshot.png"\r\n'
            f"Content-Type: image/png\r\n\r\n"
        ).encode() + file_data + f"\r\n--{boundary}--\r\n".encode()

        req_headers = dict(headers)
        req_headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"

        upload_res = client.post(
            f"/workspaces/{workspace_id}/issues/{issue_id}/attachments",
            content=body,
            headers=req_headers,
        )
        assert upload_res.status_code == 201
        att = upload_res.json()["data"]
        assert att["filename"] == "screenshot.png"
        assert att["is_image"] is True
        assert att["file_size"] == len(file_data)

    def test_comment_attachments(self, client, db):
        headers, workspace_id, _, board_list = pm.setup_board(client, db)
        issue = pm.issue(
            client,
            headers,
            workspace_id,
            board_list["id"],
            title="Comment Attachment Issue",
        )
        issue_id = issue["id"]

        # 1. Upload an attachment
        content_bytes = b"attached comment document content"
        b64 = base64.b64encode(content_bytes).decode()
        upload_res = client.post(
            f"/workspaces/{workspace_id}/issues/{issue_id}/attachments",
            json={
                "filename": "comment_notes.txt",
                "content_type": "text/plain",
                "content_base64": b64,
            },
            headers=headers,
        )
        assert upload_res.status_code == 201
        attachment_id = upload_res.json()["data"]["id"]

        # 2. Create comment linking attachment
        comment_res = client.post(
            f"/workspaces/{workspace_id}/issues/{issue_id}/comments",
            json={
                "body": "Here is the attached document",
                "attachment_ids": [attachment_id],
            },
            headers=headers,
        )
        assert comment_res.status_code == 201
        comment_data = comment_res.json()["data"]
        comment_id = comment_data["id"]
        assert len(comment_data["attachments"]) == 1
        assert comment_data["attachments"][0]["id"] == attachment_id
        assert comment_data["attachments"][0]["comment_id"] == comment_id

        # 3. Fetch issue comments and check comments have attachments
        detail_res = client.get(
            f"/workspaces/{workspace_id}/issues/{issue_id}/comments",
            headers=headers,
        )
        assert detail_res.status_code == 200
        comments = detail_res.json()["data"]
        assert len(comments) >= 1
        matched_comment = next(c for c in comments if c["id"] == comment_id)
        assert len(matched_comment["attachments"]) == 1
        assert matched_comment["attachments"][0]["filename"] == "comment_notes.txt"

        # 4. Filter attachments by comment_id
        filtered_list = client.get(
            f"/workspaces/{workspace_id}/issues/{issue_id}/attachments?comment_id={comment_id}",
            headers=headers,
        )
        assert filtered_list.status_code == 200
        assert len(filtered_list.json()["data"]) == 1
        assert filtered_list.json()["data"][0]["id"] == attachment_id

        # 5. Delete comment and ensure attachment is soft deleted
        del_comment = client.delete(
            f"/workspaces/{workspace_id}/issues/{issue_id}/comments/{comment_id}",
            headers=headers,
        )
        assert del_comment.status_code == 200
        assert del_comment.json()["data"]["id"] == comment_id

        att_check = client.get(
            f"/workspaces/{workspace_id}/issues/{issue_id}/attachments?comment_id={comment_id}",
            headers=headers,
        )
        assert len(att_check.json()["data"]) == 0

    def test_pdf_and_image_inline_download_and_query_token(self, client, db):
        headers, workspace_id, _, board_list = pm.setup_board(client, db)
        issue = pm.issue(
            client, headers, workspace_id, board_list["id"], title="Preview Test Issue"
        )
        issue_id = issue["id"]

        pdf_bytes = b"%PDF-1.4 fake pdf content"
        b64_pdf = base64.b64encode(pdf_bytes).decode()
        upload_res = client.post(
            f"/workspaces/{workspace_id}/issues/{issue_id}/attachments",
            json={
                "filename": "specification.pdf",
                "content_type": "application/pdf",
                "content_base64": b64_pdf,
            },
            headers=headers,
        )
        assert upload_res.status_code == 201
        att = upload_res.json()["data"]
        assert att["download_url"].startswith(
            f"/api/workspaces/{workspace_id}/issues/{issue_id}/attachments/{att['id']}/download"
        )

        # 1. Download PDF via header auth -> inline disposition
        dl_res = client.get(
            f"/workspaces/{workspace_id}/issues/{issue_id}/attachments/{att['id']}/download",
            headers=headers,
        )
        assert dl_res.status_code == 200
        assert dl_res.content == pdf_bytes
        assert 'inline; filename="specification.pdf"' in dl_res.headers["content-disposition"]
        assert dl_res.headers.get("x-frame-options") == "SAMEORIGIN"

        # 2. Download PDF via query param token without Authorization header
        token = headers["Authorization"].split(" ")[1]
        dl_query_res = client.get(
            f"/workspaces/{workspace_id}/issues/{issue_id}/attachments/{att['id']}/download?token={token}"
        )
        assert dl_query_res.status_code == 200
        assert dl_query_res.content == pdf_bytes
        assert 'inline; filename="specification.pdf"' in dl_query_res.headers["content-disposition"]


