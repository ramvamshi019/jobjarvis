"""Security utilities: JWT, password hashing, PII masking."""
import re
import hashlib
from datetime import datetime, timedelta
from typing import Optional, Any

from jose import JWTError, jwt
import bcrypt
from app.config import settings

# PII patterns to mask in logs
_PII_PATTERNS = [
    (re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'), '[EMAIL]'),
    (re.compile(r'\b(\+?1?\s*[-.]?\(?\d{3}\)?[-.]?\s*\d{3}[-.]?\d{4})\b'), '[PHONE]'),
    (re.compile(r'\bSSN[:\s]*\d{3}-?\d{2}-?\d{4}\b', re.IGNORECASE), '[SSN]'),
    (re.compile(r'\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b'), '[CARD]'),
]


def mask_pii(text: str) -> str:
    """Mask PII in log text. Never log phone/email/resume content."""
    if not text:
        return text
    for pattern, replacement in _PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def get_password_hash(password: str) -> str:
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode('utf-8'), salt)
    return hashed.decode('utf-8')


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))
    except ValueError:
        return False


def create_access_token(data: dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload
    except JWTError:
        return None


def fingerprint_job(normalized_title: str, company_id: int, normalized_location: str) -> str:
    """Create a deterministic fingerprint for dedup."""
    raw = f"{normalized_title}::{company_id}::{normalized_location}".lower()
    return hashlib.sha256(raw.encode()).hexdigest()[:64]


def hash_content(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:64]
