# Cloud Storage MCP Server

This MCP server provides unified Model Context Protocol tools for uploading, downloading, deleting, and verifying object files across **AWS S3**, **Azure Blob Storage**, and **Local Filesystem** storage.

## Tools
- `storage_upload_file`: Uploads base64 file data to cloud object storage. If cloud credentials are not supplied, invalid, or unreachable, automatically falls back to local storage.
- `storage_download_file`: Retrieves stored file data as bytes/base64 from the specified provider.
- `storage_delete_file`: Removes stored files from the corresponding bucket, container, or disk directory.
- `storage_test_connection`: Performs a lightweight connectivity check (e.g. S3 HEAD/List or Azure container properties) to validate customer-supplied credentials.

## Security & Architecture
- Uses AWS Signature Version 4 (AWS4-HMAC-SHA256) for AWS S3 authentication.
- Uses SharedKey HMAC-SHA256 for Azure Blob Storage authentication.
- Implements strict tenant isolation using workspace ID directory prefixing.
- Safely sanitizes filenames and MIME types to prevent path traversal or XSS exploits.
