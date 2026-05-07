"""Auth schemas — with graceful EmailStr fallback if email-validator is absent."""
from typing import Optional
from pydantic import BaseModel

# EmailStr requires the 'email-validator' package. We import it safely so the
# server starts even if the package is missing — signup will still validate
# basic string format via the fallback validator in auth.py.
try:
    from pydantic import EmailStr as _EmailStr
    _email_type = _EmailStr
except ImportError:
    _email_type = str  # type: ignore[assignment]


class SignupRequest(BaseModel):
    email: _email_type  # type: ignore[valid-type]
    password: str
    full_name: Optional[str] = None


class LoginRequest(BaseModel):
    email: _email_type  # type: ignore[valid-type]
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: int
    email: str
    role: str


class UserProfile(BaseModel):
    id: int
    email: str
    full_name: Optional[str]
    role: str
    is_active: bool
    target_roles: Optional[list]
    open_to_remote: bool
    work_authorization: Optional[str]

    model_config = {"from_attributes": True}
