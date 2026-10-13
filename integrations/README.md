# Integrations

Outbound transports. Today that is one: `aws_ses.py`, the SMTP mail transport used for OTP
verification, invitations and password resets. It is optional: with no `SMTP_HOST` the
message is logged instead of sent, and the request still succeeds.

A transport a derived project adds belongs here too, and should keep the same shape:
configured entirely by environment, absent by default, and degrading one capability at the
moment it is used rather than failing the import.

---

# `aws_ses.py`

The SMTP transport (use AWS SES's SMTP endpoint in production, or any other SMTP server) for
OTP and registration mail (`controllers/auth_controller.register_controller`,
`resend_otp_controller`), invitations (`controllers/user_controller.add_user_controller`)
and password resets (`controllers/auth_controller.handle_forgot_password`).

Configured entirely by environment: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`,
`SMTP_PASSWORD`, `SMTP_FROM_EMAIL`, `SMTP_FROM_NAME` (default "Base Project") and
`SMTP_USE_TLS`. With no host or no from-address configured, it logs the message at INFO
instead of sending it, rather than failing the request — which is what makes a fresh install
usable before anyone has an SMTP account.

The logged copy is **not** a substitute for the invite and reset emails. Their links carry
a live one-time token as `?token=…`, and the log redaction filter (`core/redact.py`) masks it
— deliberately: a log line is exactly where a credential must not survive. OTP codes are not
masked, so self-registration can be completed from the log; invites and resets cannot. To
follow those links locally, run a mail catcher and point SMTP at it:

```bash
docker run --rm -p 1025:1025 -p 8025:8025 axllent/mailpit
# .env: SMTP_HOST=localhost SMTP_PORT=1025 SMTP_USE_TLS=false SMTP_FROM_EMAIL=dev@example.com
```

and read the mail at <http://localhost:8025>.
