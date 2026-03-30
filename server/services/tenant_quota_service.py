# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
# 
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""
Tenant Quota Service: Rate limiting, circuit breaker, and quota enforcement.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import TenantQuota

logger = logging.getLogger(__name__)


class QuotaExceededException(Exception):
    """Raised when a tenant exceeds their quota."""

    pass


class CircuitBreakerOpenException(Exception):
    """Raised when circuit breaker is open."""

    pass


class TenantQuotaService:
    """Service for enforcing rate limits and circuit breaker logic."""

    # Circuit breaker thresholds
    CIRCUIT_BREAKER_THRESHOLD = 10  # Failures before opening
    CIRCUIT_BREAKER_TIMEOUT = 300  # 5 minutes in seconds
    HALF_OPEN_RETRY_LIMIT = 3  # Max retries in half-open state

    def __init__(self):
        pass

    def get_or_create_quota(
        self, db: Session, user_id: int, provider: str
    ) -> TenantQuota:
        """
        Get existing quota or create default quota for user+provider.

        Default quotas:
        - Daily: 1000 requests
        - Monthly: 30000 requests
        - Concurrent: 5 requests
        """
        stmt = select(TenantQuota).where(
            TenantQuota.user_id == user_id, TenantQuota.provider == provider
        )
        quota = db.scalar(stmt)

        if not quota:
            quota = TenantQuota(
                user_id=user_id,
                provider=provider,
                daily_quota=1000,
                monthly_quota=30000,
                concurrent_limit=5,
                daily_usage=0,
                monthly_usage=0,
                concurrent_usage=0,
                circuit_state="closed",
                circuit_failure_count=0,
                circuit_last_failure=None,
                circuit_half_open_successes=0,
            )
            db.add(quota)
            db.commit()
            db.refresh(quota)
            logger.info(
                f"Created default quota for user {user_id}, provider {provider}"
            )

        return quota

    def check_and_increment(
        self, db: Session, user_id: int, provider: str
    ) -> TenantQuota:
        """
        Check if request is allowed and increment usage counters.

        Raises:
            QuotaExceededException: If daily, monthly, or concurrent limit exceeded
            CircuitBreakerOpenException: If circuit breaker is open

        Returns:
            Updated quota record
        """
        quota = self.get_or_create_quota(db, user_id, provider)

        # Check circuit breaker
        self._check_circuit_breaker(quota)

        # Reset daily counter if needed
        if quota.daily_reset_at and quota.daily_reset_at < datetime.utcnow():
            quota.daily_usage = 0
            quota.daily_reset_at = datetime.utcnow() + timedelta(days=1)

        # Reset monthly counter if needed
        if quota.monthly_reset_at and quota.monthly_reset_at < datetime.utcnow():
            quota.monthly_usage = 0
            quota.monthly_reset_at = datetime.utcnow() + timedelta(days=30)

        # Check quotas
        if quota.daily_usage >= quota.daily_quota:
            logger.warning(f"User {user_id} exceeded daily quota for {provider}")
            raise QuotaExceededException(f"Daily quota of {quota.daily_quota} exceeded")

        if quota.monthly_usage >= quota.monthly_quota:
            logger.warning(f"User {user_id} exceeded monthly quota for {provider}")
            raise QuotaExceededException(
                f"Monthly quota of {quota.monthly_quota} exceeded"
            )

        if quota.concurrent_usage >= quota.concurrent_limit:
            logger.warning(f"User {user_id} exceeded concurrent limit for {provider}")
            raise QuotaExceededException(
                f"Concurrent limit of {quota.concurrent_limit} exceeded"
            )

        # Increment counters
        quota.daily_usage += 1
        quota.monthly_usage += 1
        quota.concurrent_usage += 1

        db.commit()
        db.refresh(quota)

        return quota

    def decrement_concurrent(self, db: Session, user_id: int, provider: str):
        """Decrement concurrent usage counter after request completes."""
        stmt = select(TenantQuota).where(
            TenantQuota.user_id == user_id, TenantQuota.provider == provider
        )
        quota = db.scalar(stmt)

        if quota and quota.concurrent_usage > 0:
            quota.concurrent_usage -= 1
            db.commit()

    def record_success(self, db: Session, user_id: int, provider: str):
        """Record successful request for circuit breaker logic."""
        stmt = select(TenantQuota).where(
            TenantQuota.user_id == user_id, TenantQuota.provider == provider
        )
        quota = db.scalar(stmt)

        if not quota:
            return

        if quota.circuit_state == "half_open":
            quota.circuit_half_open_successes += 1
            if quota.circuit_half_open_successes >= self.HALF_OPEN_RETRY_LIMIT:
                # Close the circuit
                quota.circuit_state = "closed"
                quota.circuit_failure_count = 0
                quota.circuit_half_open_successes = 0
                logger.info(
                    f"Circuit breaker closed for user {user_id}, provider {provider}"
                )
        elif quota.circuit_state == "closed":
            # Reset failure count on success
            quota.circuit_failure_count = 0

        db.commit()

    def record_failure(self, db: Session, user_id: int, provider: str):
        """Record failed request for circuit breaker logic."""
        stmt = select(TenantQuota).where(
            TenantQuota.user_id == user_id, TenantQuota.provider == provider
        )
        quota = db.scalar(stmt)

        if not quota:
            return

        quota.circuit_failure_count += 1
        quota.circuit_last_failure = datetime.utcnow()

        if quota.circuit_state == "half_open":
            # If half_open fails, go back to open
            quota.circuit_state = "open"
            quota.circuit_half_open_successes = 0
            logger.warning(
                f"Circuit breaker reopened for user {user_id}, provider {provider}"
            )
        elif (
            quota.circuit_state == "closed"
            and quota.circuit_failure_count >= self.CIRCUIT_BREAKER_THRESHOLD
        ):
            # Open the circuit
            quota.circuit_state = "open"
            logger.warning(
                f"Circuit breaker opened for user {user_id}, provider {provider} "
                f"after {quota.circuit_failure_count} failures"
            )

        db.commit()

    def _check_circuit_breaker(self, quota: TenantQuota):
        """Check circuit breaker state and raise exception if open."""
        if quota.circuit_state == "open":
            # Check if timeout expired
            if quota.circuit_last_failure:
                timeout_expired = (
                    datetime.utcnow() - quota.circuit_last_failure
                ).total_seconds() > self.CIRCUIT_BREAKER_TIMEOUT

                if timeout_expired:
                    # Move to half_open
                    quota.circuit_state = "half_open"
                    quota.circuit_half_open_successes = 0
                    logger.info(
                        f"Circuit breaker half-open for user {quota.user_id}, "
                        f"provider {quota.provider}"
                    )
                else:
                    raise CircuitBreakerOpenException(
                        f"Circuit breaker is open for {quota.provider}. "
                        f"Retry after timeout."
                    )
            else:
                raise CircuitBreakerOpenException(
                    f"Circuit breaker is open for {quota.provider}"
                )

    def get_usage_stats(self, db: Session, user_id: int, provider: str) -> dict | None:
        """Get current usage statistics for a user+provider."""
        quota = self.get_or_create_quota(db, user_id, provider)

        return {
            "provider": provider,
            "daily_usage": quota.daily_usage,
            "daily_quota": quota.daily_quota,
            "daily_remaining": max(0, quota.daily_quota - quota.daily_usage),
            "monthly_usage": quota.monthly_usage,
            "monthly_quota": quota.monthly_quota,
            "monthly_remaining": max(0, quota.monthly_quota - quota.monthly_usage),
            "concurrent_usage": quota.concurrent_usage,
            "concurrent_limit": quota.concurrent_limit,
            "circuit_state": quota.circuit_state,
            "circuit_failure_count": quota.circuit_failure_count,
        }

    def update_quotas(
        self,
        db: Session,
        user_id: int,
        provider: str,
        daily_quota: int | None = None,
        monthly_quota: int | None = None,
        concurrent_limit: int | None = None,
    ) -> TenantQuota:
        """Update quota limits for a user+provider."""
        quota = self.get_or_create_quota(db, user_id, provider)

        if daily_quota is not None:
            quota.daily_quota = daily_quota
        if monthly_quota is not None:
            quota.monthly_quota = monthly_quota
        if concurrent_limit is not None:
            quota.concurrent_limit = concurrent_limit

        db.commit()
        db.refresh(quota)

        logger.info(
            f"Updated quotas for user {user_id}, provider {provider}: "
            f"daily={quota.daily_quota}, monthly={quota.monthly_quota}, "
            f"concurrent={quota.concurrent_limit}"
        )

        return quota
