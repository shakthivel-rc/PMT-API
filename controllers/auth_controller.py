from fastapi import Response, status
from fastapi.responses import JSONResponse
from schemas.user import UserLoginRequest, UserRegisterRequest, PasswordSchema, ForgotPasswordSchema, VerifyOTPRequest, ResendOTPRequest, SetPasswordAfterRegRequest, RefreshTokenRequest
from schemas.response import SuccessResponse, ErrorResponse
from schemas.email_schema import EmailTemplate
from sqlalchemy.orm import Session
from services.user import get_user_by_username, get_user_by_email
import datetime
import logging
import secrets
import bcrypt
import jwt
import uuid
from dotenv import load_dotenv
from core.config import get_settings
from utils.token import decode_base64, decrypt_string, generate_random_code, encode_to_base64, encrypt_string
from utils.token import RESET_LINK_PATH, app_link
from utils.password import hash_password, validate_password_policy, verify_password
from models.user import User
from integrations.aws_ses import send_email
from models.user_session import UserSession, hash_session_token
from utils.constants import reset_password_email_template, otp_email_template
from services.activity_log import log_activity

load_dotenv()

settings = get_settings()
SECRET_KEY = settings.secret_key
ALGORITHM = settings.algorithm
JWT_EXPIRY_VALUE = settings.jwt_expiry_hours

logger = logging.getLogger("base_project.auth")

# A bcrypt hash of a value nobody knows and nobody will ever verify against, so that the
# unknown-account branch of login has something to spend the same time on that a real
# account spends. See `_login_failure` for why the branches have to cost the same.
#
# Computed once, at import, and not per request. Per request it would be a second bcrypt on
# every sign-in — doubling the cost of the *successful* path, the one real users wait on, to
# defend a branch that only ever fails. It costs ~0.43 s at start-up on this machine
# (measured 2026-08-27: `bcrypt.hashpw` at the shipped 12 rounds), once, in the same
# import phase that already builds the settings object and the Fernet cipher.
#
# Generated rather than pasted in as a literal, so it always carries the *current* work
# factor: `hash_password` is what wrote every real password hash in the database, so raising
# the cost moves this and them together. A hard-coded hash would pin the dummy comparison to
# whatever the rounds were the day it was written down, and the timing gap this exists to
# close would silently reopen the first time the real one moved.
#
# The plaintext is thrown away with the expression that made it. `secrets`, not `random`,
# per CLAUDE.md §7 rule 4 — a predictable input would let someone recompute the hash and
# confirm what it is, which does not authenticate anything but does hand them a way to tell
# the miss branch from the hit branch again.
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


#: How long an invite link works, counted from the account's creation.
INVITE_TTL = datetime.timedelta(hours=48)


def _invite_expired(user: User) -> bool:
    """Whether the invite behind `user.verify_token` is past `INVITE_TTL`.

    `created_at` is written in UTC (`models/user.py`), and this compared it with
    `datetime.now()` — local time — until 2026-09-29, so the window was 48 hours only on a
    UTC host: 42.5 in Kolkata, 56 in California. Nobody noticed because the invite link
    itself was dead until the same day. The column is naive on the way back from both
    databases, so the comparison is made naive-UTC to naive-UTC; an aware value (a row still
    in the identity map from the request that created it) is converted first.
    """
    created = user.created_at
    if created.tzinfo is not None:
        created = created.astimezone(datetime.UTC).replace(tzinfo=None)
    return _utcnow().replace(tzinfo=None) > created + INVITE_TTL


def _password_policy_error(password: str):
    errors = validate_password_policy(password)
    if errors:
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(
                status_code=400,
                message="Password does not meet policy requirements",
                data={"errors": errors},
            ).model_dump(),
        )
    return None

def create_jwt_token(user_id: str) -> dict:
    """Generate distinct access and refresh JWTs."""
    now = _utcnow()
    access_expires_at = now + datetime.timedelta(hours=JWT_EXPIRY_VALUE)
    refresh_expires_at = now + datetime.timedelta(days=settings.refresh_token_expiry_days)
    access_payload = {
        "sub": user_id,
        "type": "access",
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": access_expires_at,
    }
    refresh_payload = {
        "sub": user_id,
        "type": "refresh",
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": refresh_expires_at,
    }
    access_token = jwt.encode(access_payload, SECRET_KEY, algorithm=ALGORITHM)
    refresh_token = jwt.encode(refresh_payload, SECRET_KEY, algorithm=ALGORITHM)
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        # Both hashes are computed here, next to the tokens they belong to, so that the two
        # call sites that write a session row (login below, and refresh) cannot each decide
        # for themselves what goes in the columns. `user_sessions` stores neither raw token.
        "access_token_hash": hash_session_token(access_token),
        "refresh_token_hash": hash_session_token(refresh_token),
        "refresh_token_expires_at": refresh_expires_at.replace(tzinfo=None),
    }


# One status and one message for every way a sign-in can fail, so that the response cannot
# be read as an answer to "is there an account at this address?".
#
# Until 2026-08-27 this endpoint gave three distinguishable answers: 401 "Invalid username
# or password" for an unknown account, 401 "Verify your account to login" for a known but
# unverified one, and 423 "Account is temporarily locked" for a known locked one. Two of the
# three confirm that an address is registered, to anybody who can post a form (SEC-6).
#
# The 423 went the same way as the wording and for the same reason: five wrong guesses
# answering 401,401,401,401,**423** against a real address and 401,401,401,401,401 against
# an invented one is the same oracle, written in status codes instead of English.
#
# **The honest cost.** "Verify your account to login" is genuinely useful, and the person it
# was useful to is now told their password is wrong. The usual resolution is to send that
# sentence by email — to the address that would have received it anyway, which tells an
# attacker nothing — and that is the right follow-up here; this is not it. Gating the
# friendlier message behind a *correct* password instead would not have worked for this
# service either: a self-registered account that has not verified often has no password at
# all yet (see `register_controller`), so there is no credential it could pass first.
# The lockout message is the same trade, and its loss is sharper — a locked-out user now
# has no way to learn that waiting is what fixes it.
_LOGIN_FAILURE_MESSAGE = "Invalid username or password"


def _login_failure(reason: str, username: str, user_id: str | None = None) -> JSONResponse:
    """The one response every failed sign-in gets, and the log line that says which one.

    `reason` goes to the operator and never to the caller, and that asymmetry is the whole
    point: an attacker with a stopwatch and a wordlist sees one answer, while support
    answering "why can I not log in?" can still find out that the account exists, is
    unverified, or is locked.

    The submitted `username` is logged because it is the only handle an operator has on a
    sign-in that never resolved to an account — "someone is walking our address book" is not
    a question the `user_id` field can answer, since there is no user.
    """
    logger.info("Login rejected", extra={"extra_fields": {
        "event": "login_rejected",
        "reason": reason,
        "username": username,
        "user_id": user_id,
    }})
    error_response = ErrorResponse(status_code=401, message=_LOGIN_FAILURE_MESSAGE)
    return JSONResponse(status_code=401, content=error_response.model_dump())


def login_controller(user: UserLoginRequest, db: Session, ip_address: str | None = None,
                     user_agent: str | None = None) -> SuccessResponse:
    # Try lookup by email first, then by username
    user_details = get_user_by_email(db, user.username)
    if not user_details:
        user_details = get_user_by_username(db=db, username=user.username)
    if not user_details:
        # Spend the bcrypt this branch has no stored hash to spend it on, before answering.
        # Without it the miss returns in microseconds while a hit spends ~0.43 s in
        # `checkpw` (measured 2026-08-27, 12 rounds), and that difference is the same
        # enumeration oracle the wording used to be — readable with a stopwatch, immune to
        # anything the response body says, and needing exactly one request per address. The
        # result is discarded on purpose; only the time it took is wanted.
        verify_password(user.password, _DUMMY_PASSWORD_HASH)
        return _login_failure("no such account", user.username)

    now = datetime.datetime.utcnow()
    # Verified first and unconditionally, before any branch decides anything. The unverified
    # and locked branches used to return *before* reaching bcrypt, which made them measurably
    # faster than a wrong password against a healthy account and told an attacker which
    # addresses were which.
    #
    # `user_details.password` is NULL for an account that registered and never set one, and
    # `verify_password` answers False for that rather than raising — but it answers
    # instantly, so those rows are compared against the dummy hash instead. They can never
    # match it, which is the correct answer for an account with no password.
    password_matches = verify_password(user.password,
                                       user_details.password or _DUMMY_PASSWORD_HASH)

    # The branch order below is the one this endpoint has always had — unverified, then
    # locked, then wrong password — kept deliberately so that only the *answers* changed and
    # not which accounts get their failure counter incremented. In particular an unverified
    # account still cannot be locked out by guesses at a password it does not have, and so
    # cannot arrive at verification already serving a lockout.
    if not user_details.status:
        return _login_failure("account not verified", user.username, user_details.id)

    if user_details.locked_until and user_details.locked_until > now:
        return _login_failure("account locked", user.username, user_details.id)

    if not password_matches:
        user_details.failed_login_attempts = (user_details.failed_login_attempts or 0) + 1
        user_details.last_failed_login_at = now
        reason = "wrong password"
        if user_details.failed_login_attempts >= settings.auth_max_failed_login_attempts:
            user_details.locked_until = now + datetime.timedelta(minutes=settings.auth_lockout_minutes)
            # The lockout is still recorded and still enforced — it is only invisible in the
            # response. `failed_login_attempts` and `locked_until` are what an operator reads
            # and what the branch above enforces on the next attempt.
            reason = "wrong password, lockout tripped"
        db.commit()
        return _login_failure(reason, user.username, user_details.id)

    token = create_jwt_token(user_details.id)
    #Generate the JWT with access and refresh token

    user_session = UserSession(
        user_id=user_details.id,
        access_token_hash=token["access_token_hash"],
        refresh_token_hash=token["refresh_token_hash"],
        refresh_token_expires_at=token["refresh_token_expires_at"],
        ip_address=ip_address,
        user_agent=user_agent,
    )
    db.add(user_session)

    user_details.failed_login_attempts = 0
    user_details.locked_until = None
    user_details.last_failed_login_at = None

    user_display_name = f"{user_details.first_name} {user_details.last_name}"
    log_activity(db, user_details.id, user_display_name, "LOGIN", details="Successful login", ip_address=ip_address)

    db.commit()
    db.refresh(user_session)

    return SuccessResponse(status="success", status_code=200, message="Login successful", data={"access_token": token["access_token"], "refresh_token": token["refresh_token"]})


def refresh_token_controller(schema: RefreshTokenRequest, db: Session) -> SuccessResponse:
    try:
        payload = jwt.decode(schema.refresh_token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        error_response = ErrorResponse(status_code=401, message="Refresh token has expired")
        return JSONResponse(status_code=401, content=error_response.model_dump())
    except jwt.InvalidTokenError:
        error_response = ErrorResponse(status_code=401, message="Invalid refresh token")
        return JSONResponse(status_code=401, content=error_response.model_dump())

    if payload.get("type") != "refresh":
        error_response = ErrorResponse(status_code=401, message="Invalid token type")
        return JSONResponse(status_code=401, content=error_response.model_dump())

    refresh_token_hash = hash_session_token(schema.refresh_token)
    session = db.query(UserSession).filter(
        UserSession.refresh_token_hash == refresh_token_hash,
        UserSession.revoked_at.is_(None),
    ).first()
    if not session:
        error_response = ErrorResponse(status_code=401, message="Invalid or revoked refresh token")
        return JSONResponse(status_code=401, content=error_response.model_dump())

    if session.refresh_token_expires_at and session.refresh_token_expires_at < datetime.datetime.utcnow():
        session.revoked_at = datetime.datetime.utcnow()
        db.commit()
        error_response = ErrorResponse(status_code=401, message="Refresh token has expired")
        return JSONResponse(status_code=401, content=error_response.model_dump())

    token = create_jwt_token(session.user_id)
    session.access_token_hash = token["access_token_hash"]
    session.refresh_token_hash = token["refresh_token_hash"]
    session.refresh_token_expires_at = token["refresh_token_expires_at"]
    db.commit()

    return SuccessResponse(
        status="success",
        status_code=200,
        message="Token refreshed successfully",
        data={"access_token": token["access_token"], "refresh_token": token["refresh_token"]},
    )

# verify if the token is valid
def verify_token(response: Response, token: str, db: Session):
    decrypted_token = ""
    try:
        decoded_token = decode_base64(token)
        decrypted_token = decrypt_string(decoded_token)
    except Exception:
        response.status_code = status.HTTP_400_BAD_REQUEST
        return ErrorResponse(status="error", status_code=400, message="Invalid Token", data={})
    user = db.query(User).filter(User.verify_token == decrypted_token).first()
    if not user:
        response.status_code = status.HTTP_404_NOT_FOUND
        return ErrorResponse(status="error", status_code=400, message="User doesn't exists with this token")

    # check if the token is expired
    if _invite_expired(user):
        response.status_code = status.HTTP_400_BAD_REQUEST
        return ErrorResponse(status="error", status_code=400, message="The verification token is expired")
       
    return SuccessResponse(status="success", status_code=200, message="user verified successfully", data={})

# verify if the token is valid
def verify_forgot_password_token(response: Response, token: str, db: Session):
    decrypted_token = ""
    try:
        decoded_token = decode_base64(token)
        decrypted_token = decrypt_string(decoded_token)
    except Exception:
        response.status_code = status.HTTP_400_BAD_REQUEST
        return ErrorResponse(status="error", status_code=400, message="Invalid Token", data={})
    user = db.query(User).filter(User.fp_token == decrypted_token).first()
    if not user:
        response.status_code = status.HTTP_404_NOT_FOUND
        return ErrorResponse(status="error", status_code=400, message="User doesn't exists with this token")

    # check if the token is expired
    if datetime.datetime.now() > (user.fp_token_created_at + datetime.timedelta(hours=48)):
        response.status_code = status.HTTP_400_BAD_REQUEST
        return ErrorResponse(status="error", status_code=400, message="The verification token is expired")
       
    return SuccessResponse(status="success", status_code=200, message="user verified successfully", data={})

# to set the password
def set_user_password(response: Response, token: str, password_schema: PasswordSchema, db: Session):
    decrypted_token = ""
    try:
        decoded_token = decode_base64(token)
        decrypted_token = decrypt_string(decoded_token)
    except Exception:
        response.status_code = status.HTTP_400_BAD_REQUEST
        return ErrorResponse(status="error", status_code=400, message="Invalid Token", data={})
    user = db.query(User).filter(User.verify_token == decrypted_token).first()
    if not user:
        response.status_code = status.HTTP_404_NOT_FOUND
        return ErrorResponse(status="error", status_code=400, message="User doesn't exists with this token")

    if _invite_expired(user):
        response.status_code = status.HTTP_400_BAD_REQUEST
        return ErrorResponse(status="error", status_code=400, message="The verification token is expired")

    # check if new password and confirm password are the same
    if password_schema.new_password != password_schema.confirm_password:
        response.status_code = status.HTTP_400_BAD_REQUEST
        return ErrorResponse(status="error", status_code=400, message="Passwords don't match", data=None)

    password_error = _password_policy_error(password_schema.new_password)
    if password_error:
        return password_error

    user.password = hash_password(password_schema.new_password) 
    user.status = True
    user.verify_token = None
    db.commit()

    response.status_code = status.HTTP_201_CREATED
    return SuccessResponse(status="success", message="Password set successfully", status_code=201, data={})

# send email for the user to reset password
def handle_forgot_password(response: Response, schema: ForgotPasswordSchema, db: Session):
    user = get_user_by_email(db, schema.email)
    if not user:
        response.status_code = status.HTTP_404_NOT_FOUND
        return ErrorResponse(status="error", status_code=response.status_code, message="No user found with this email")
    
    if not user.status:
        response.status_code = status.HTTP_400_BAD_REQUEST
        return ErrorResponse(status="error", status_code=response.status_code, message="Unverified users cannot reset their password")
    
    # setting up the email
    token = generate_random_code(16)
    encoded_token = encode_to_base64(encrypt_string(token))
    reset_password_url = app_link(RESET_LINK_PATH, encoded_token)
    email_template = EmailTemplate(subject="Reset Your Base Project Password",body=reset_password_email_template(user.first_name, reset_password_url))
    # sending the email
    email_response = send_email(recipient_email=user.email,email_template=email_template)
    # throw error when email can not be sent
    if not email_response["status"]:
        response.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        return ErrorResponse(status="error", status_code=500, message="Error occurred sending the email. make sure the email you entered is correct")

    user.fp_token = token
    user.fp_token_created_at = datetime.datetime.now()
    db.commit()
    response.status_code = status.HTTP_200_OK
    return SuccessResponse(status="success", status_code=response.status_code, message="Reset password email sent successfully", data={})

# To reset the password
def handle_reset_password(response: Response, token: str, schema: PasswordSchema, db:Session):
    decrypted_token = ""
    try:
        decoded_token = decode_base64(token)
        decrypted_token = decrypt_string(decoded_token)
    except Exception:
        response.status_code = status.HTTP_400_BAD_REQUEST
        return ErrorResponse(status="error", status_code=400, message="Invalid Token", data={})
    user = db.query(User).filter(User.fp_token == decrypted_token).first()
    if not user:
        response.status_code = status.HTTP_404_NOT_FOUND
        return ErrorResponse(status="error", status_code=400, message="User doesn't exists with this token")

    # check if the token is expired
    if datetime.datetime.now() > (user.fp_token_created_at + datetime.timedelta(hours=48)):
        response.status_code = status.HTTP_400_BAD_REQUEST
        return ErrorResponse(status="error", status_code=400, message="The verification token is expired")

    # check if new password and confirm password are the same
    if schema.new_password != schema.confirm_password:
        response.status_code = status.HTTP_400_BAD_REQUEST
        return ErrorResponse(status="error", status_code=400, message="Passwords don't match", data=None)

    password_error = _password_policy_error(schema.new_password)
    if password_error:
        return password_error

    user.password = hash_password(schema.new_password) 
    user.fp_token = None
    user.fp_token_created_at = None
    db.query(UserSession).filter(UserSession.user_id == user.id).update(
        {UserSession.revoked_at: datetime.datetime.utcnow()},
        synchronize_session=False,
    )
    db.commit()

    response.status_code = status.HTTP_201_CREATED
    return SuccessResponse(status="success", message="Password set successfully", status_code=201, data={})

def logout_controller(request, db):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return ErrorResponse(status="error", status_code=401, message="Missing or invalid token")

    token = auth_header.split(" ")[1]
    # By hash, like every other read of this table: the raw token is not stored, so there is
    # nothing else to match on. Revoked rows are deliberately *not* excluded here — logging
    # out of a session that is already revoked should still remove the row rather than
    # answer "invalid session".
    session = db.query(UserSession).filter(
        UserSession.access_token_hash == hash_session_token(token)
    ).first()

    if not session:
        return ErrorResponse(status_code=400, message="Invalid session or already logged out")

    # Log activity before deleting session
    user_payload = getattr(request.state, "user", None)
    if user_payload:
        user_id = user_payload.get("sub")
        user = db.query(User).filter(User.id == user_id).first()
        user_name = f"{user.first_name} {user.last_name}" if user else ""
        ip_address = request.client.host if request.client else None
        log_activity(db, user_id, user_name, "LOGOUT", details="User logged out", ip_address=ip_address)

    # Delete the session from the database
    db.delete(session)
    db.commit()

    return SuccessResponse(
        status="success",
        message="Logged out successfully",
        status_code=200,
        data={}
    )


def _generate_otp() -> str:
    """A 6-digit OTP, from the cryptographic generator.

    `random.randint` was here, and `random` is the Mersenne Twister: fully deterministic
    from its state, and its state is recoverable from enough outputs. An OTP is a second
    authentication factor, so predicting it is the whole attack — `secrets` is the same
    line of code and is seeded from the OS.

    Range spelled as `randbelow(900000) + 100000` because `secrets` has no `randint`;
    it produces exactly the same closed interval [100000, 999999], uniformly.
    """
    return str(secrets.randbelow(900_000) + 100_000)


def _hash_otp(otp: str) -> str:
    """Hash OTP using bcrypt."""
    return bcrypt.hashpw(otp.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def _verify_otp(plain_otp: str, hashed_otp: str) -> bool:
    """Verify OTP against its hash."""
    return bcrypt.checkpw(plain_otp.encode('utf-8'), hashed_otp.encode('utf-8'))


def register_controller(response: Response, data: UserRegisterRequest, db: Session):
    """Self-registration step 1: collect info, generate OTP, send email."""
    # Check if email exists and is already verified
    existing_email = get_user_by_email(db, data.email)
    if existing_email and existing_email.status:
        response.status_code = status.HTTP_400_BAD_REQUEST
        return ErrorResponse(status="error", status_code=400, message="Email already registered")

    # If unverified user exists with same email, remove it to allow re-registration
    if existing_email and not existing_email.status:
        db.delete(existing_email)
        db.flush()

    # Check username uniqueness (excluding soft-deleted and the just-deleted unverified)
    existing_username = get_user_by_username(db, data.username)
    if existing_username:
        response.status_code = status.HTTP_400_BAD_REQUEST
        return ErrorResponse(status="error", status_code=400, message="Username already taken")

    # Generate OTP
    otp = _generate_otp()
    hashed_otp = _hash_otp(otp)

    # Create user without password, status=False (unverified)
    new_user = User(
        email=data.email,
        first_name=data.first_name,
        last_name=data.last_name,
        username=data.username,
        password=None,
        organization=data.organization,
        department=data.department,
        status=False,
        otp_code=hashed_otp,
        otp_created_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add(new_user)
    db.flush()

    # Send OTP email
    email_template = EmailTemplate(
        subject="Your Base Project Verification Code",
        body=otp_email_template(data.first_name, otp)
    )
    email_response = send_email(recipient_email=data.email, email_template=email_template)
    if not email_response["status"]:
        db.rollback()
        response.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        return ErrorResponse(status="error", status_code=500, message="Failed to send verification email. Please try again.")

    db.commit()

    response.status_code = status.HTTP_201_CREATED
    return SuccessResponse(
        status="success",
        status_code=201,
        message="OTP sent to your email. Please verify to continue.",
        data={"email": data.email}
    )


def verify_otp_controller(response: Response, data: VerifyOTPRequest, db: Session):
    """Verify the 6-digit OTP sent during registration."""
    user = db.query(User).filter(
        User.email == data.email,
        User.status.is_(False),
        User.deleted == 0
    ).first()

    if not user:
        response.status_code = status.HTTP_404_NOT_FOUND
        return ErrorResponse(status="error", status_code=404, message="No pending registration found for this email")

    if not user.otp_code or not user.otp_created_at:
        response.status_code = status.HTTP_400_BAD_REQUEST
        return ErrorResponse(status="error", status_code=400, message="No OTP found. Please request a new one.")

    # Check OTP expiry (10 minutes)
    now = datetime.datetime.now(datetime.timezone.utc)
    otp_created = user.otp_created_at.replace(tzinfo=datetime.timezone.utc) if user.otp_created_at.tzinfo is None else user.otp_created_at
    if now > (otp_created + datetime.timedelta(minutes=10)):
        response.status_code = status.HTTP_400_BAD_REQUEST
        return ErrorResponse(status="error", status_code=400, message="OTP has expired. Please request a new one.")

    # Verify OTP
    if not _verify_otp(data.otp, user.otp_code):
        response.status_code = status.HTTP_400_BAD_REQUEST
        return ErrorResponse(status="error", status_code=400, message="Invalid OTP. Please try again.")

    return SuccessResponse(
        status="success",
        status_code=200,
        message="OTP verified successfully. Please set your password.",
        data={"verified": True, "email": data.email}
    )


def resend_otp_controller(response: Response, data: ResendOTPRequest, db: Session):
    """Resend OTP for an unverified registration."""
    user = db.query(User).filter(
        User.email == data.email,
        User.status.is_(False),
        User.deleted == 0
    ).first()

    if not user:
        response.status_code = status.HTTP_404_NOT_FOUND
        return ErrorResponse(status="error", status_code=404, message="No pending registration found for this email")

    # Rate limit: 60 seconds between resends
    if user.otp_created_at:
        now = datetime.datetime.now(datetime.timezone.utc)
        otp_created = user.otp_created_at.replace(tzinfo=datetime.timezone.utc) if user.otp_created_at.tzinfo is None else user.otp_created_at
        elapsed = (now - otp_created).total_seconds()
        if elapsed < 60:
            remaining = int(60 - elapsed)
            response.status_code = status.HTTP_429_TOO_MANY_REQUESTS
            return ErrorResponse(status="error", status_code=429, message=f"Please wait {remaining} seconds before requesting a new OTP")

    # Generate and store new OTP
    otp = _generate_otp()
    user.otp_code = _hash_otp(otp)
    user.otp_created_at = datetime.datetime.now(datetime.timezone.utc)

    # Send OTP email
    email_template = EmailTemplate(
        subject="Your Base Project Verification Code",
        body=otp_email_template(user.first_name, otp)
    )
    email_response = send_email(recipient_email=user.email, email_template=email_template)
    if not email_response["status"]:
        db.rollback()
        response.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        return ErrorResponse(status="error", status_code=500, message="Failed to send verification email. Please try again.")

    db.commit()

    return SuccessResponse(
        status="success",
        status_code=200,
        message="New OTP sent to your email.",
        data={"email": data.email}
    )


def set_password_after_registration_controller(response: Response, data: SetPasswordAfterRegRequest, db: Session):
    """Final registration step: set password after OTP verification."""
    user = db.query(User).filter(
        User.email == data.email,
        User.status.is_(False),
        User.deleted == 0
    ).first()

    if not user:
        response.status_code = status.HTTP_404_NOT_FOUND
        return ErrorResponse(status="error", status_code=404, message="No pending registration found for this email")

    # Re-verify OTP is still valid
    if not user.otp_code or not user.otp_created_at:
        response.status_code = status.HTTP_400_BAD_REQUEST
        return ErrorResponse(status="error", status_code=400, message="No OTP found. Please restart registration.")

    now = datetime.datetime.now(datetime.timezone.utc)
    otp_created = user.otp_created_at.replace(tzinfo=datetime.timezone.utc) if user.otp_created_at.tzinfo is None else user.otp_created_at
    if now > (otp_created + datetime.timedelta(minutes=10)):
        response.status_code = status.HTTP_400_BAD_REQUEST
        return ErrorResponse(status="error", status_code=400, message="OTP has expired. Please restart registration.")

    if not _verify_otp(data.otp, user.otp_code):
        response.status_code = status.HTTP_400_BAD_REQUEST
        return ErrorResponse(status="error", status_code=400, message="Invalid OTP.")

    # Validate passwords match
    if data.new_password != data.confirm_password:
        response.status_code = status.HTTP_400_BAD_REQUEST
        return ErrorResponse(status="error", status_code=400, message="Passwords don't match")

    password_error = _password_policy_error(data.new_password)
    if password_error:
        return password_error

    # Set password and activate user
    user.password = hash_password(data.new_password)
    user.status = True
    user.otp_code = None
    user.otp_created_at = None

    # Assign the default "Member" role (seeded by seeders/user_seeder.py)
    from models.roles import Role
    from models.user_roles import UserRole
    default_role = db.query(Role).filter(Role.name == "Member").first()
    if default_role:
        user_role = UserRole(user_id=user.id, role_id=default_role.id)
        db.add(user_role)

    log_activity(db, user.id, f"{user.first_name} {user.last_name}", "CREATE", entity_type="user", entity_id=user.id, details="Self-registration completed")

    db.commit()

    response.status_code = status.HTTP_201_CREATED
    return SuccessResponse(
        status="success",
        status_code=201,
        message="Account created successfully. You can now log in.",
        data={"user_id": user.id}
    )