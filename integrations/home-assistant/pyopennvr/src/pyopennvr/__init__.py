"""Async client for the OpenNVR API (contract 1.x).

Kept free of Home Assistant imports so it can be published and reused on its
own (and so the HA integration meets HA's dependency-transparency rule).
"""

from .client import SUPPORTED_CONTRACT_MAJOR, OpenNVRClient, check_contract
from .events import CLOSE_TOKEN_REVOKED, EventStream
from .exceptions import (
    OpenNVRAuthError,
    OpenNVRConnectionError,
    OpenNVRContractError,
    OpenNVRError,
    OpenNVRNotFoundError,
    OpenNVRRequestError,
)
from .models import (
    Camera,
    EntityCatalog,
    EntityDescriptor,
    SignedMedia,
    SiteMode,
    StreamInfo,
    SystemInfo,
    Zone,
)
from .whep import Whep, WhepSession, resolve_session_url

__version__ = "0.1.0"

__all__ = [
    "CLOSE_TOKEN_REVOKED", "SUPPORTED_CONTRACT_MAJOR", "Camera", "EntityCatalog",
    "EntityDescriptor", "EventStream", "OpenNVRAuthError", "OpenNVRClient",
    "OpenNVRConnectionError", "OpenNVRContractError", "OpenNVRError", "OpenNVRNotFoundError",
    "OpenNVRRequestError", "SignedMedia", "SiteMode", "StreamInfo", "SystemInfo", "Whep",
    "WhepSession", "Zone", "check_contract", "resolve_session_url",
]
