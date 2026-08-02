"""
LinkedIn OAuth handler — Backward Compatibility Alias.

Delegates to `linkedin_page.py` as the default handler for legacy "linkedin" platform requests.
For explicit platform flows, see:
  - linkedin_page.py    : Organization / Company Pages (Community Management API)
  - linkedin_profile.py : Personal Profiles (Share on LinkedIn + OpenID)
"""
from .linkedin_page import (
    build_auth_url,
    exchange_token,
    discover_accounts,
    refresh_token,
)

__all__ = [
    "build_auth_url",
    "exchange_token",
    "discover_accounts",
    "refresh_token",
]
