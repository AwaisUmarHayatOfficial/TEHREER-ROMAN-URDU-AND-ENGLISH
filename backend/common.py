"""Small helpers shared by the backend modules."""
import os
from fastapi import Request

TRUST_PROXY = os.getenv("TRUST_PROXY", "0") == "1"
# Header the reverse proxy sets (overwriting any client value) with the real client IP
PROXY_IP_HEADER = os.getenv("PROXY_IP_HEADER", "x-forwarded-for").lower()


def client_ip(request: Request) -> str:
    if TRUST_PROXY:
        value = request.headers.get(PROXY_IP_HEADER, "")
        if value:
            return value.split(",")[0].strip()[:45]
    return (request.client.host if request.client else "unknown")[:45]


def is_https(request: Request) -> bool:
    """True when the visitor reached us over HTTPS (directly or through the reverse proxy)."""
    if TRUST_PROXY and request.headers.get("x-forwarded-proto", "").lower() == "https":
        return True
    return request.url.scheme == "https"
