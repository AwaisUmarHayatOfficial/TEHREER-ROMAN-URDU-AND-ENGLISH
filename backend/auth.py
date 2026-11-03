"""
Accounts: register with a password, sign in with Gmail + password or with Google,
verify the Gmail address with an e-mailed code, reset a forgotten password by e-mail.

Endpoints (all under /api/auth)
  GET  /config            public settings for the forms (Google client id, password rules)
  POST /register          create a pending account and e-mail a 6-digit code
  POST /verify-email      confirm the code -> account active + signed in
  POST /resend-code       e-mail a new verification code
  POST /login             Gmail + password
  GET  /google/start      "Continue with Google": redirects the browser to Google
  GET  /google/callback   Google sends the user back here; the account is created / found and signed in
  POST /forgot-password   e-mail a reset code
  POST /reset-password    code + new password
  POST /change-password  (signed in) current + new password
  GET  /me, POST /logout

Security notes
  * Gmail addresses only (ALLOWED_EMAIL_DOMAINS); every address is proven by an e-mailed code or by Google
  * passwords: Argon2id hashes, complexity rules, account lock after repeated failures
  * codes: stored only as an HMAC, 10 minute expiry, limited attempts, resend cooldown, hourly limits
  * the session cookie is a random token (HttpOnly, SameSite=Lax, Secure over HTTPS); only its SHA-256 is stored
  * responses for sign-in failures and password resets do not reveal whether an account exists
"""
import hashlib
import hmac
import logging
import os
import re
import secrets
import smtplib
import ssl
import time
from collections import defaultdict, deque
from email.message import EmailMessage
from email.utils import formataddr
from typing import Optional
from urllib.parse import urlencode

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from . import db
from .common import client_ip, is_https

log = logging.getLogger("tehreer.auth")

BRAND = os.getenv("BRAND", "Tehreer")
SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)
    log.warning("SECRET_KEY is not set: using a random key (codes requested before a restart become invalid). Set SECRET_KEY in .env")

CODE_TTL_MINUTES = int(os.getenv("CODE_TTL_MINUTES", "10"))
RESEND_SECONDS = int(os.getenv("CODE_RESEND_SECONDS", "60"))
MAX_CODES_PER_HOUR = int(os.getenv("MAX_CODES_PER_HOUR", "5"))
MAX_CODE_ATTEMPTS = 5
MAX_WRONG_CODES_PER_HOUR = 10
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "30"))
COOKIE_NAME = "tehreer_session"
ALLOWED_DOMAINS = [d.strip().lower() for d in os.getenv("ALLOWED_EMAIL_DOMAINS", "gmail.com,googlemail.com").split(",") if d.strip()]

PASSWORD_MIN = int(os.getenv("PASSWORD_MIN_LENGTH", "10"))
PASSWORD_MAX = 64
MAX_LOGIN_FAILURES = int(os.getenv("MAX_LOGIN_FAILURES", "5"))
LOCK_MINUTES = int(os.getenv("LOCK_MINUTES", "15"))

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "").strip()  # optional override; default: <this site>/api/auth/google/callback
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
OAUTH_COOKIE = "tehreer_oauth"

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "") or SMTP_USER
# starttls (port 587, default) | ssl (port 465) | none (only for local test servers)
SMTP_SECURITY = os.getenv("SMTP_SECURITY", "ssl" if SMTP_PORT == 465 else "starttls").lower()
ALLOW_DEV_CODES = os.getenv("ALLOW_DEV_CODES", "0") == "1"

EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]{1,64}@[a-z0-9.\-]{1,190}\.[a-z]{2,}$")
NAME_RE = re.compile(r"[^\W\d_]+(?:[ '.\-][^\W\d_]+)*")
USERNAME_RE = re.compile(r"[a-z][a-z0-9_.]{2,29}")
RESERVED_USERNAMES = {"admin", "administrator", "root", "support", "help", "system", "staff", "moderator", "info", "contact",
                      "security", "noreply", "postmaster", "webmaster", "tehreer", "api", "login", "register"}
COMMON_PASSWORDS = {"password", "passw0rd", "qwerty", "letmein", "welcome", "iloveyou", "admin", "abc123", "123456", "12345678",
                    "1234567890", "qwertyuiop", "password1", "tehreer"}

PURPOSE_LABEL = {"verify_email": "verification", "reset_password": "password reset"}

router = APIRouter(prefix="/api/auth", tags=["auth"])
_ip_hist: dict = defaultdict(deque)
ph = PasswordHasher()
DUMMY_HASH = ph.hash("dummy-password-used-to-equalise-timing")


# ------------------------------------------------------------------ helpers
def _ip_limit(kind: str, ip: str, limit: int, window: int = 3600):
    now = time.time()
    h = _ip_hist[(kind, ip)]
    while h and h[0] < now - window:
        h.popleft()
    if len(h) >= limit:
        raise HTTPException(429, "Too many attempts from your network. Please try again later.")
    h.append(now)


def normalize_email(raw: str):
    """Returns (clean, normalized). Gmail: dots and +tags are ignored so one mailbox = one account."""
    clean = (raw or "").strip().lower()
    if not EMAIL_RE.match(clean):
        raise ValueError("Enter a valid Gmail address.")
    local, domain = clean.rsplit("@", 1)
    if ALLOWED_DOMAINS and domain not in ALLOWED_DOMAINS:
        raise ValueError("Please use your Gmail address (name@gmail.com). Other e-mail providers are not accepted.")
    norm_domain = "gmail.com" if domain in ("gmail.com", "googlemail.com") else domain
    norm_local = local
    if norm_domain == "gmail.com":
        norm_local = local.split("+", 1)[0].replace(".", "")
    if not norm_local:
        raise ValueError("Enter a valid Gmail address.")
    return clean, f"{norm_local}@{norm_domain}"


def clean_name(raw: str, label: str) -> str:
    s = re.sub(r"\s+", " ", (raw or "").strip())
    if not s or len(s) > 60 or not NAME_RE.fullmatch(s):
        raise ValueError(f"{label} may only contain letters, spaces, hyphens and apostrophes (up to 60 characters).")
    return s


def clean_phone(raw: str) -> str:
    s = re.sub(r"[\s\-().]", "", raw or "")
    if not re.fullmatch(r"\+?\d{10,15}", s):
        raise ValueError("Enter a valid phone number with 10 to 15 digits, for example 03001234567 or +923001234567.")
    return s


def clean_username(raw: str) -> str:
    s = (raw or "").strip().lower()
    if not USERNAME_RE.fullmatch(s):
        raise ValueError("Username must be 3 to 30 characters: start with a letter, then letters, numbers, dots or underscores.")
    if s in RESERVED_USERNAMES:
        raise ValueError("That username is reserved. Please choose another.")
    return s


def check_password(pw: str, email_local: str = "", username: str = "") -> Optional[str]:
    """Returns an error message, or None when the password is acceptable."""
    pw = pw or ""
    if len(pw) < PASSWORD_MIN:
        return f"Password must be at least {PASSWORD_MIN} characters."
    if len(pw) > PASSWORD_MAX:
        return f"Password must be at most {PASSWORD_MAX} characters."
    if not re.search(r"[a-z]", pw):
        return "Password needs at least one lowercase letter."
    if not re.search(r"[A-Z]", pw):
        return "Password needs at least one uppercase letter."
    if not re.search(r"\d", pw):
        return "Password needs at least one number."
    if not re.search(r"[^A-Za-z0-9]", pw):
        return "Password needs at least one symbol (for example ! @ # $ %)."
    low = pw.lower()
    if low in COMMON_PASSWORDS:
        return "That password is too common. Please choose another."
    for part in (username, email_local):
        part = (part or "").lower()
        if len(part) >= 4 and part in low:
            return "Password must not contain your username or the name part of your e-mail."
    return None


def _code_hash(norm: str, purpose: str, code: str) -> str:
    return hmac.new(SECRET_KEY.encode(), f"{purpose}:{norm}:{code}".encode(), hashlib.sha256).hexdigest()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _event(event: str, norm: Optional[str], ip: str, ua: str = "", user_id: Optional[int] = None):
    try:
        db.execute(
            "INSERT INTO auth_events (user_id, email_normalized, event, ip, user_agent) VALUES (%s,%s,%s,%s,%s)",
            (user_id, norm, event, ip, (ua or "")[:255]),
        )
    except Exception:  # auditing must never break sign-in
        log.exception("could not write auth event")


def public_user(u: dict) -> dict:
    created = u.get("created_at")
    first, last = u.get("first_name") or "", u.get("last_name") or ""
    return {
        "id": u["id"],
        "email": u["email"],
        "first_name": first,
        "last_name": last,
        "username": u.get("username") or "",
        "phone": u.get("phone") or "",
        "display_name": (f"{first} {last}".strip() or u.get("display_name") or u.get("username") or u["email"].split("@")[0]),
        "avatar_url": u["avatar_url"] if (u.get("avatar_url") or "").startswith("https://") else "",
        "has_password": bool(u.get("password_hash")),
        "google_linked": bool(u.get("google_sub")),
        "created_at": created.isoformat() + "Z" if created else None,
    }


# ------------------------------------------------------------------ e-mail
def _email_bodies(code: str, purpose: str):
    mins = CODE_TTL_MINUTES
    if purpose == "reset_password":
        heading, intro = "Reset your password", "Use this code to reset your password:"
    else:
        heading, intro = "Confirm your e-mail", "Use this code to confirm your e-mail and finish creating your account:"
    text = (f"{heading}\n\n{intro} {code}\n\nIt expires in {mins} minutes. "
            f"If you did not ask for it, you can ignore this e-mail.\n")
    html = f"""<!doctype html><html><body style="margin:0;background:#f5f7fa;font-family:Arial,Helvetica,sans-serif;color:#1a2433">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:32px 16px">
<table role="presentation" width="480" cellpadding="0" cellspacing="0" style="max-width:480px;background:#ffffff;border:1px solid #d5dce6;border-radius:12px;overflow:hidden">
<tr><td style="background:#1f3a5f;padding:20px 28px;color:#ffffff;font-size:20px;font-weight:bold">{BRAND}</td></tr>
<tr><td style="padding:28px">
<h2 style="margin:0 0 12px;font-size:20px;color:#1f3a5f">{heading}</h2>
<p style="margin:0 0 12px;font-size:16px">{intro}</p>
<p style="margin:0 0 20px;font-size:36px;letter-spacing:8px;font-weight:bold;color:#1f3a5f">{code}</p>
<p style="margin:0 0 8px;font-size:14px;color:#5f6b7a">The code expires in {mins} minutes and can be used once.</p>
<p style="margin:0;font-size:14px;color:#5f6b7a">If you did not request it, you can safely ignore this e-mail.</p>
</td></tr></table></td></tr></table></body></html>"""
    return text, html


def _send_code_email(to_addr: str, code: str, purpose: str):
    if not SMTP_HOST:
        if ALLOW_DEV_CODES:
            log.warning("DEV MODE - %s code for %s is %s", PURPOSE_LABEL.get(purpose, purpose), to_addr, code)
            return
        raise RuntimeError("email_not_configured")
    text, html = _email_bodies(code, purpose)
    msg = EmailMessage()
    msg["Subject"] = f"{code} is your {BRAND} {PURPOSE_LABEL.get(purpose, 'verification')} code"
    msg["From"] = formataddr((BRAND, SMTP_FROM))
    msg["To"] = to_addr
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    ctx = ssl.create_default_context()
    if SMTP_SECURITY == "ssl":
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20, context=ctx)
    else:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)
        if SMTP_SECURITY == "starttls":
            server.starttls(context=ctx)
    try:
        if SMTP_USER:
            server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


def _issue_code(clean: str, norm: str, purpose: str, ip: str, send: bool = True) -> dict:
    """Create (and e-mail) a code. With send=False the same limits apply but nothing is sent, so callers
    that must not reveal whether an account exists behave identically in both cases."""
    stats = db.one(
        "SELECT COUNT(*) AS c, TIMESTAMPDIFF(SECOND, MAX(created_at), UTC_TIMESTAMP()) AS since "
        "FROM login_codes WHERE email_normalized = %s AND purpose = %s AND created_at > UTC_TIMESTAMP() - INTERVAL 1 HOUR",
        (norm, purpose),
    )
    if stats["since"] is not None and stats["since"] < RESEND_SECONDS:
        wait = RESEND_SECONDS - int(stats["since"])
        raise HTTPException(429, f"Please wait {wait} seconds before asking for a new code.", headers={"Retry-After": str(wait)})
    if stats["c"] >= MAX_CODES_PER_HOUR:
        raise HTTPException(429, "Too many codes were requested for this address. Please try again in an hour.")
    code = f"{secrets.randbelow(10 ** 6):06d}"
    row_id, _ = db.execute(
        "INSERT INTO login_codes (email_normalized, purpose, code_hash, ip, expires_at) "
        "VALUES (%s, %s, %s, %s, UTC_TIMESTAMP() + INTERVAL %s MINUTE)",
        (norm, purpose, _code_hash(norm, purpose, code), ip, CODE_TTL_MINUTES),
    )
    if send:
        try:
            _send_code_email(clean, code, purpose)
        except RuntimeError:
            db.execute("DELETE FROM login_codes WHERE id = %s", (row_id,))
            raise HTTPException(503, "E-mail is not configured on this server yet.")
        except Exception:
            log.exception("could not send e-mail")
            db.execute("DELETE FROM login_codes WHERE id = %s", (row_id,))
            raise HTTPException(503, "We could not send the e-mail right now. Please try again in a minute.")
    return {"ok": True, "resend_in": RESEND_SECONDS, "expires_in": CODE_TTL_MINUTES * 60,
            "dev_mode": (not SMTP_HOST) and ALLOW_DEV_CODES}


def _check_code(norm: str, purpose: str, raw_code: str, ip: str, ua: str):
    """Validates and consumes a code, or raises HTTPException."""
    code = re.sub(r"\D", "", raw_code or "")
    if len(code) != 6:
        raise HTTPException(400, "Enter the 6-digit code from the e-mail.")
    wrong = db.one(
        "SELECT COALESCE(SUM(attempts), 0) AS n FROM login_codes WHERE email_normalized = %s AND purpose = %s "
        "AND created_at > UTC_TIMESTAMP() - INTERVAL 1 HOUR", (norm, purpose))
    if int(wrong["n"]) >= MAX_WRONG_CODES_PER_HOUR:
        raise HTTPException(429, "Too many incorrect codes. Please try again in an hour.")
    row = db.one(
        "SELECT id, code_hash, attempts FROM login_codes WHERE email_normalized = %s AND purpose = %s "
        "AND used_at IS NULL AND expires_at > UTC_TIMESTAMP() ORDER BY id DESC LIMIT 1", (norm, purpose))
    if not row:
        _event("code_expired", norm, ip, ua)
        raise HTTPException(400, "That code has expired or was not requested. Please request a new code.")
    if row["attempts"] >= MAX_CODE_ATTEMPTS:
        raise HTTPException(400, "Too many wrong attempts. Please request a new code.")
    if not hmac.compare_digest(row["code_hash"], _code_hash(norm, purpose, code)):
        db.execute("UPDATE login_codes SET attempts = attempts + 1 WHERE id = %s", (row["id"],))
        _event("code_wrong", norm, ip, ua)
        left = MAX_CODE_ATTEMPTS - row["attempts"] - 1
        raise HTTPException(400, (f"Incorrect code. {left} attempt{'s' if left != 1 else ''} left.") if left > 0 else "Incorrect code. Please request a new code.")
    db.execute("UPDATE login_codes SET used_at = UTC_TIMESTAMP() WHERE email_normalized = %s AND purpose = %s AND used_at IS NULL", (norm, purpose))


# ------------------------------------------------------------------ sessions
def current_user(request: Request) -> Optional[dict]:
    token = request.cookies.get(COOKIE_NAME)
    if not token or len(token) > 100:
        return None
    row = db.one(
        "SELECT u.*, s.id AS sid, TIMESTAMPDIFF(MINUTE, s.last_seen_at, UTC_TIMESTAMP()) AS idle_min "
        "FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = %s AND s.expires_at > UTC_TIMESTAMP()",
        (_token_hash(token),),
    )
    if not row or row["status"] != "active":
        return None
    if row["idle_min"] and row["idle_min"] >= 10:
        db.execute("UPDATE sessions SET last_seen_at = UTC_TIMESTAMP() WHERE id = %s", (row["sid"],))
    return row


def require_user(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Please sign in to continue.")
    return user


def _start_session(user_id: int, request: Request, response: Response):
    token = secrets.token_urlsafe(32)
    db.execute(
        "INSERT INTO sessions (user_id, token_hash, ip, user_agent, expires_at) "
        "VALUES (%s, %s, %s, %s, UTC_TIMESTAMP() + INTERVAL %s DAY)",
        (user_id, _token_hash(token), client_ip(request), request.headers.get("user-agent", "")[:255], SESSION_DAYS),
    )
    response.set_cookie(COOKIE_NAME, token, max_age=SESSION_DAYS * 86400, httponly=True,
                        samesite="lax", secure=is_https(request), path="/")


def verify_user_password(user: dict, password: str) -> bool:
    if not user.get("password_hash"):
        return False
    try:
        return ph.verify(user["password_hash"], (password or "")[:256])
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# ------------------------------------------------------------------ request bodies
class EmailBody(BaseModel):
    email: str


class RegisterBody(BaseModel):
    first_name: str
    last_name: str
    phone: str
    email: str
    username: str
    password: str


class VerifyBody(BaseModel):
    email: str
    code: str


class LoginBody(BaseModel):
    email: str
    password: str


class ResetBody(BaseModel):
    email: str
    code: str
    password: str


class ChangePasswordBody(BaseModel):
    current_password: str = ""
    new_password: str


# ------------------------------------------------------------------ endpoints
@router.get("/config")
def config():
    return {"google_enabled": google_enabled(), "password_min": PASSWORD_MIN, "password_max": PASSWORD_MAX,
            "allowed_domains": ALLOWED_DOMAINS, "code_minutes": CODE_TTL_MINUTES}


@router.post("/register")
def register(body: RegisterBody, request: Request):
    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")
    _ip_limit("register", ip, 15)
    try:
        clean, norm = normalize_email(body.email)
        first = clean_name(body.first_name, "First name")
        last = clean_name(body.last_name, "Last name")
        phone = clean_phone(body.phone)
        username = clean_username(body.username)
    except ValueError as e:
        raise HTTPException(400, str(e))
    problem = check_password(body.password, norm.split("@")[0], username)
    if problem:
        raise HTTPException(400, problem)

    existing = db.one("SELECT id, status FROM users WHERE email_normalized = %s", (norm,))
    if existing and existing["status"] != "pending":
        raise HTTPException(409, "An account with this Gmail address already exists. Sign in, or use Forgot password.")
    taken = db.one("SELECT id FROM users WHERE username = %s", (username,))
    if taken and (not existing or taken["id"] != existing["id"]):
        raise HTTPException(409, "That username is already taken. Please choose another.")

    pwh = ph.hash(body.password)
    display = f"{first} {last}"
    try:
        if existing:  # an unfinished registration for this address: replace it
            db.execute(
                "UPDATE users SET email = %s, first_name = %s, last_name = %s, username = %s, phone = %s, display_name = %s, "
                "password_hash = %s, created_at = UTC_TIMESTAMP() WHERE id = %s",
                (clean, first, last, username, phone, display, pwh, existing["id"]))
        else:
            db.execute(
                "INSERT INTO users (email, email_normalized, first_name, last_name, username, phone, display_name, password_hash, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending')",
                (clean, norm, first, last, username, phone, display, pwh))
    except db.IntegrityError:
        raise HTTPException(409, "That username or Gmail address is already registered.")
    result = _issue_code(clean, norm, "verify_email", ip)
    _event("register", norm, ip, ua)
    result["email"] = clean
    return result


@router.post("/resend-code")
def resend_code(body: EmailBody, request: Request):
    ip = client_ip(request)
    _ip_limit("resend", ip, 20)
    try:
        clean, norm = normalize_email(body.email)
    except ValueError as e:
        raise HTTPException(400, str(e))
    user = db.one("SELECT status FROM users WHERE email_normalized = %s", (norm,))
    return _issue_code(clean, norm, "verify_email", ip, send=bool(user and user["status"] == "pending"))


@router.post("/verify-email")
def verify_email(body: VerifyBody, request: Request, response: Response):
    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")
    _ip_limit("verify", ip, 40)
    try:
        _, norm = normalize_email(body.email)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _check_code(norm, "verify_email", body.code, ip, ua)
    user = db.one("SELECT * FROM users WHERE email_normalized = %s", (norm,))
    if not user:
        raise HTTPException(400, "We could not find that registration. Please register again.")
    if user["status"] == "blocked":
        raise HTTPException(403, "This account has been disabled. Please contact support.")
    db.execute(
        "UPDATE users SET status = 'active', email_verified_at = COALESCE(email_verified_at, UTC_TIMESTAMP()), "
        "login_count = login_count + 1, last_login_at = UTC_TIMESTAMP(), failed_logins = 0, locked_until = NULL WHERE id = %s",
        (user["id"],))
    user = db.one("SELECT * FROM users WHERE id = %s", (user["id"],))
    _start_session(user["id"], request, response)
    _event("email_verified", norm, ip, ua, user["id"])
    return {"user": public_user(user)}


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response):
    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")
    _ip_limit("login", ip, 60)
    try:
        clean, norm = normalize_email(body.email)
    except ValueError as e:
        raise HTTPException(400, str(e))
    pw = (body.password or "")[:256]
    user = db.one(
        "SELECT *, (locked_until IS NOT NULL AND locked_until > UTC_TIMESTAMP()) AS is_locked, "
        "TIMESTAMPDIFF(MINUTE, UTC_TIMESTAMP(), locked_until) AS lock_min FROM users WHERE email_normalized = %s", (norm,))
    if user and user["is_locked"]:
        raise HTTPException(429, f"Too many failed attempts. Please try again in {max(1, int(user['lock_min'] or 0) + 1)} minutes, or reset your password.")

    if not user or not user["password_hash"]:
        try:  # keep the timing similar whether or not the account exists
            ph.verify(DUMMY_HASH, pw)
        except Exception:
            pass
        _event("login_failed", norm, ip, ua)
        if user and user["google_sub"]:
            raise HTTPException(401, "This account uses Google sign-in. Use \"Continue with Google\", or choose Forgot password to set a password.")
        raise HTTPException(401, "Incorrect e-mail or password.")

    if not verify_user_password(user, pw):
        fails = user["failed_logins"] + 1
        if fails >= MAX_LOGIN_FAILURES:
            db.execute("UPDATE users SET failed_logins = 0, locked_until = UTC_TIMESTAMP() + INTERVAL %s MINUTE WHERE id = %s", (LOCK_MINUTES, user["id"]))
        else:
            db.execute("UPDATE users SET failed_logins = %s WHERE id = %s", (fails, user["id"]))
        _event("login_failed", norm, ip, ua, user["id"])
        raise HTTPException(401, "Incorrect e-mail or password.")

    if user["status"] == "blocked":
        raise HTTPException(403, "This account has been disabled. Please contact support.")
    if user["status"] == "pending":
        try:
            _issue_code(clean, norm, "verify_email", ip)
        except HTTPException:
            pass  # cooldown: a code was sent a moment ago
        raise HTTPException(403, "Please confirm your e-mail first. We sent a 6-digit code to your Gmail.", headers={"X-Error-Code": "email_not_verified"})

    new_hash = ph.hash(pw) if ph.check_needs_rehash(user["password_hash"]) else user["password_hash"]
    db.execute("UPDATE users SET failed_logins = 0, locked_until = NULL, password_hash = %s, login_count = login_count + 1, "
               "last_login_at = UTC_TIMESTAMP() WHERE id = %s", (new_hash, user["id"]))
    _start_session(user["id"], request, response)
    _event("login_ok", norm, ip, ua, user["id"])
    return {"user": public_user(user)}


def google_enabled() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def _safe_next(n: Optional[str]) -> str:
    return n if (n and re.fullmatch(r"/(?!/)[^\\\s]*", n)) else "/app"


def _sign(value: str) -> str:
    return hmac.new(SECRET_KEY.encode(), value.encode(), hashlib.sha256).hexdigest()[:32]


def _redirect_uri(request: Request) -> str:
    if GOOGLE_REDIRECT_URI:
        return GOOGLE_REDIRECT_URI
    host = request.headers.get("host", "localhost")
    return f"{'https' if is_https(request) else 'http'}://{host}/api/auth/google/callback"


def google_fetch_identity(code: str, redirect_uri: str, nonce: str) -> dict:
    """Exchanges the authorization code for tokens and returns the verified ID-token claims.
    (Replaced in tests.) Raises on any problem."""
    import requests as http
    from google.auth.transport import requests as g_requests
    from google.oauth2 import id_token
    r = http.post(GOOGLE_TOKEN_URL, data={
        "code": code, "client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code"}, timeout=15)
    r.raise_for_status()
    raw = r.json().get("id_token")
    if not raw:
        raise ValueError("no id_token")
    info = id_token.verify_oauth2_token(raw, g_requests.Request(), GOOGLE_CLIENT_ID, clock_skew_in_seconds=10)
    if info.get("nonce") != nonce:
        raise ValueError("nonce mismatch")
    return info


def _unique_username(base: str) -> str:
    base = re.sub(r"[^a-z0-9_.]", "", base.lower()) or "user"
    if not base[0].isalpha():
        base = "u" + base
    base = base[:22]
    if len(base) < 3:
        base += "user"
    candidate = base
    for _ in range(20):
        if candidate not in RESERVED_USERNAMES and not db.one("SELECT 1 AS x FROM users WHERE username = %s", (candidate,)):
            return candidate
        candidate = f"{base}{secrets.randbelow(10000):04d}"
    return f"user{secrets.token_hex(6)}"


class GoogleError(Exception):
    def __init__(self, code: str):
        self.code = code


def _google_account(info: dict) -> dict:
    """Finds or creates the account for a verified Google identity. Raises GoogleError."""
    if not info.get("email_verified"):
        raise GoogleError("google_failed")
    try:
        clean, norm = normalize_email(info.get("email", ""))
    except ValueError:
        raise GoogleError("google_not_gmail")
    sub = str(info.get("sub", ""))[:64]
    if not sub:
        raise GoogleError("google_failed")
    first = (info.get("given_name") or "").strip()[:60] or None
    last = (info.get("family_name") or "").strip()[:60] or None
    if not first and info.get("name"):
        first = str(info["name"]).strip()[:60] or None
    picture = (info.get("picture") or "")[:500] or None
    if picture and not picture.startswith("https://"):
        picture = None

    user = db.one("SELECT * FROM users WHERE google_sub = %s", (sub,)) or db.one("SELECT * FROM users WHERE email_normalized = %s", (norm,))
    if user:
        if user["status"] == "blocked":
            raise GoogleError("google_blocked")
        if user["google_sub"] and user["google_sub"] != sub:
            raise GoogleError("google_failed")
        display = user["display_name"] or (f"{first or ''} {last or ''}".strip() or None)
        db.execute(
            "UPDATE users SET google_sub = %s, status = 'active', email_verified_at = COALESCE(email_verified_at, UTC_TIMESTAMP()), "
            "first_name = COALESCE(first_name, %s), last_name = COALESCE(last_name, %s), display_name = %s, "
            "avatar_url = COALESCE(%s, avatar_url), login_count = login_count + 1, last_login_at = UTC_TIMESTAMP(), "
            "failed_logins = 0, locked_until = NULL WHERE id = %s",
            (sub, first, last, display, picture, user["id"]))
        if not user["username"]:
            db.execute("UPDATE users SET username = %s WHERE id = %s", (_unique_username(norm.split("@")[0]), user["id"]))
    else:
        try:
            db.execute(
                "INSERT INTO users (email, email_normalized, first_name, last_name, username, display_name, google_sub, avatar_url, "
                "status, email_verified_at, login_count, last_login_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'active',UTC_TIMESTAMP(),1,UTC_TIMESTAMP())",
                (clean, norm, first, last, _unique_username(norm.split("@")[0]), f"{first or ''} {last or ''}".strip() or None, sub, picture))
        except db.IntegrityError:
            pass  # two tabs signed in at the same moment: the row exists now
    return db.one("SELECT * FROM users WHERE google_sub = %s", (sub,)) or db.one("SELECT * FROM users WHERE email_normalized = %s", (norm,))


def _login_redirect(error: str) -> RedirectResponse:
    resp = RedirectResponse(f"/login?error={error}", status_code=302)
    resp.delete_cookie(OAUTH_COOKIE, path="/")
    return resp


@router.get("/google/start")
def google_start(request: Request, next: str = "/app"):
    """The "Continue with Google" button points here: send the browser to Google's account chooser."""
    if not google_enabled():
        log.warning("Continue with Google was clicked but is not configured: set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env (free, see README)")
        return _login_redirect("google_unconfigured")
    _ip_limit("google", client_ip(request), 60)
    log.info("Google sign-in: the Authorized redirect URI registered in Google Cloud Console must be exactly: %s", _redirect_uri(request))
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    nxt = _safe_next(next)
    payload = f"{state}.{nonce}.{nxt.encode().hex()}"
    params = {"client_id": GOOGLE_CLIENT_ID, "redirect_uri": _redirect_uri(request), "response_type": "code",
              "scope": "openid email profile", "state": state, "nonce": nonce, "prompt": "select_account"}
    resp = RedirectResponse(f"{GOOGLE_AUTH_URL}?{urlencode(params)}", status_code=302)
    resp.set_cookie(OAUTH_COOKIE, f"{payload}.{_sign(payload)}", max_age=600, httponly=True, samesite="lax",
                    secure=is_https(request), path="/")
    return resp


@router.get("/google/callback")
def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")
    if not google_enabled():
        return _login_redirect("google_unconfigured")
    if error:
        return _login_redirect("google_denied")
    cookie = request.cookies.get(OAUTH_COOKIE, "")
    parts = cookie.split(".")
    if len(parts) != 4:
        return _login_redirect("google_failed")
    c_state, c_nonce, c_next, c_sig = parts
    if not hmac.compare_digest(_sign(f"{c_state}.{c_nonce}.{c_next}"), c_sig) or not hmac.compare_digest(c_state, state or "") or not code:
        return _login_redirect("google_failed")
    try:
        nxt = _safe_next(bytes.fromhex(c_next).decode())
    except ValueError:
        nxt = "/app"
    try:
        info = google_fetch_identity(code, _redirect_uri(request), c_nonce)
    except Exception:
        log.warning("Google sign-in failed", exc_info=True)
        return _login_redirect("google_failed")
    try:
        user = _google_account(info)
    except GoogleError as e:
        return _login_redirect(e.code)
    resp = RedirectResponse(nxt, status_code=302)
    _start_session(user["id"], request, resp)
    resp.delete_cookie(OAUTH_COOKIE, path="/")
    _event("google_login", user["email_normalized"], ip, ua, user["id"])
    return resp


@router.post("/forgot-password")
def forgot_password(body: EmailBody, request: Request):
    ip = client_ip(request)
    _ip_limit("forgot", ip, 15)
    try:
        clean, norm = normalize_email(body.email)
    except ValueError as e:
        raise HTTPException(400, str(e))
    user = db.one("SELECT status FROM users WHERE email_normalized = %s", (norm,))
    result = _issue_code(clean, norm, "reset_password", ip, send=bool(user and user["status"] == "active"))
    _event("password_reset_requested", norm, ip, request.headers.get("user-agent", ""))
    return result


@router.post("/reset-password")
def reset_password(body: ResetBody, request: Request):
    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")
    _ip_limit("reset", ip, 30)
    try:
        _, norm = normalize_email(body.email)
    except ValueError as e:
        raise HTTPException(400, str(e))
    user = db.one("SELECT * FROM users WHERE email_normalized = %s", (norm,))
    problem = check_password(body.password, norm.split("@")[0], (user or {}).get("username") or "")
    if problem:
        raise HTTPException(400, problem)
    _check_code(norm, "reset_password", body.code, ip, ua)
    if not user or user["status"] != "active":
        raise HTTPException(400, "That code has expired or was not requested. Please request a new code.")
    db.execute(
        "UPDATE users SET password_hash = %s, password_changed_at = UTC_TIMESTAMP(), failed_logins = 0, locked_until = NULL, "
        "email_verified_at = COALESCE(email_verified_at, UTC_TIMESTAMP()) WHERE id = %s", (ph.hash(body.password), user["id"]))
    db.execute("DELETE FROM sessions WHERE user_id = %s", (user["id"],))  # sign out everywhere
    _event("password_reset", norm, ip, ua, user["id"])
    return {"ok": True}


@router.post("/change-password")
def change_password(body: ChangePasswordBody, request: Request):
    user = require_user(request)
    ip = client_ip(request)
    _ip_limit("change", ip, 20)
    if not user.get("password_hash"):
        raise HTTPException(400, "This account has no password yet. Use Forgot password on the sign-in page to set one.")
    if not verify_user_password(user, body.current_password):
        raise HTTPException(400, "Your current password is incorrect.")
    problem = check_password(body.new_password, user["email_normalized"].split("@")[0], user.get("username") or "")
    if problem:
        raise HTTPException(400, problem)
    token = request.cookies.get(COOKIE_NAME, "")
    db.execute("UPDATE users SET password_hash = %s, password_changed_at = UTC_TIMESTAMP() WHERE id = %s", (ph.hash(body.new_password), user["id"]))
    db.execute("DELETE FROM sessions WHERE user_id = %s AND token_hash <> %s", (user["id"], _token_hash(token)))
    _event("password_changed", user["email_normalized"], ip, request.headers.get("user-agent", ""), user["id"])
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    user = require_user(request)
    return {"user": public_user(user)}


@router.post("/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        user = current_user(request)
        db.execute("DELETE FROM sessions WHERE token_hash = %s", (_token_hash(token),))
        if user:
            _event("logout", user["email_normalized"], client_ip(request), request.headers.get("user-agent", ""), user["id"])
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}
