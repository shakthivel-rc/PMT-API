import logging
import smtplib
import os
from email.mime.application import MIMEApplication
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from schemas.email_schema import EmailTemplate

logger = logging.getLogger(__name__)


def send_email(recipient_email: str, email_template: EmailTemplate,
               attachments: list[tuple[str, bytes, str]] | None = None):
    """Send email via SMTP. Falls back to logging if SMTP is not configured.

    `attachments` is a list of `(file_name, content, content_type)`, added 2026-10-05 for
    scheduled reports (services/reports/scheduler.py). With attachments the message is
    `multipart/mixed` — an HTML part plus the files; without, it is the `alternative` message
    every caller before then has sent, unchanged.
    """
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_username = os.getenv("SMTP_USERNAME", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
    from_email = os.getenv("SMTP_FROM_EMAIL", "").strip()
    from_name = os.getenv("SMTP_FROM_NAME", "Base Project").strip()
    use_tls = os.getenv("SMTP_USE_TLS", "true").lower() == "true"

    if not smtp_host or not from_email:
        logger.info("SMTP not configured — logging email instead.")
        logger.info("EMAIL — To: %s | Subject: %s", recipient_email, email_template.subject)
        logger.info("EMAIL BODY:\n%s", email_template.body)
        for name, content, _type in attachments or []:
            logger.info("EMAIL ATTACHMENT: %s (%d bytes)", name, len(content))
        return {"status": True, "message": "Email logged (SMTP not configured)"}

    try:
        msg = MIMEMultipart("mixed" if attachments else "alternative")
        msg["Subject"] = email_template.subject
        msg["From"] = f"{from_name} <{from_email}>"
        msg["To"] = recipient_email

        msg.attach(MIMEText(email_template.body, "html"))
        for name, content, content_type in attachments or []:
            subtype = content_type.split(";")[0].split("/")[-1] or "octet-stream"
            part = MIMEApplication(content, _subtype=subtype)
            part.add_header("Content-Disposition", "attachment", filename=name)
            msg.attach(part)

        def _connect_smtp(host: str, port: int, tls: bool):
            srv = smtplib.SMTP(host, port, timeout=10)
            if tls:
                srv.starttls()
            return srv

        server = None
        hosts_to_try = [smtp_host]
        if smtp_host == "host.docker.internal":
            hosts_to_try.append("127.0.0.1")
        elif smtp_host in ("localhost", "127.0.0.1") and os.path.exists("/.dockerenv"):
            hosts_to_try.append("host.docker.internal")

        last_err = None
        for h in hosts_to_try:
            try:
                server = _connect_smtp(h, smtp_port, use_tls)
                break
            except Exception as err:
                last_err = err

        if server is None:
            raise last_err or RuntimeError(f"Could not connect to SMTP server {smtp_host}:{smtp_port}")

        if smtp_username and smtp_password:
            server.login(smtp_username, smtp_password)

        server.sendmail(from_email, recipient_email, msg.as_string())
        server.quit()

        logger.info("Email sent to %s — Subject: %s", recipient_email, email_template.subject)
        return {"status": True, "message": "Email sent successfully"}
    except Exception as e:
        logger.error("Failed to send email to %s: %s", recipient_email, str(e))
        return {"status": False, "message": f"Failed to send email: {str(e)}"}
