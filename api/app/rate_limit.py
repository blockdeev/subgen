"""Rate limiting con slowapi, key por IP de origen. El límite concreto para
crear trabajos es configurable vía `SUBGEN_RATE_LIMIT_CREATE_JOB`."""
from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, default_limits=[])
