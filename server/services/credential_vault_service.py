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
Credential Vault Service: Secure storage and retrieval of cloud provider credentials.
Implements encrypted-at-rest storage using Fernet (AES-128).
"""

import hashlib
import logging
from typing import Any

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import Settings
from models import CloudProviderCredential

logger = logging.getLogger(__name__)


class CredentialVaultService:
    """Service for encrypting and managing cloud provider credentials."""

    def __init__(self, settings: Settings):
        self.settings = settings
        if not settings.credential_encryption_key:
            raise ValueError("CREDENTIAL_ENCRYPTION_KEY must be set in environment")
        try:
            self.cipher = Fernet(settings.credential_encryption_key.encode())
        except Exception as e:
            raise ValueError(f"Invalid encryption key format: {e}")

    def encrypt_token(self, plaintext_token: str) -> str:
        """Encrypt a plaintext token using Fernet."""
        return self.cipher.encrypt(plaintext_token.encode()).decode()

    def decrypt_token(self, encrypted_token: str) -> str:
        """Decrypt a Fernet-encrypted token."""
        try:
            return self.cipher.decrypt(encrypted_token.encode()).decode()
        except Exception as e:
            logger.error(f"Failed to decrypt token: {e}")
            raise ValueError("Token decryption failed")

    @staticmethod
    def hash_token(plaintext_token: str) -> str:
        """Generate SHA-256 hash of first 16 characters for lookup."""
        prefix = plaintext_token[:16] if len(plaintext_token) >= 16 else plaintext_token
        return hashlib.sha256(prefix.encode()).hexdigest()

    def store_credential(
        self,
        db: Session,
        user_id: int,
        provider: str,
        plaintext_token: str,
        account_info: dict[str, Any] | None = None,
    ) -> CloudProviderCredential:
        """
        Store a new encrypted credential.

        Args:
            db: Database session
            user_id: Owner of the credential
            provider: Provider name (e.g., 'huggingface')
            plaintext_token: Raw token to encrypt
            account_info: Optional JSON metadata

        Returns:
            Newly created credential record
        """
        encrypted_token = self.encrypt_token(plaintext_token)
        token_hash = self.hash_token(plaintext_token)

        import json

        credential = CloudProviderCredential(
            user_id=user_id,
            provider=provider,
            encrypted_token=encrypted_token,
            token_hash=token_hash,
            encryption_key_id="default",  # For key rotation support
            account_info=json.dumps(account_info or {}),  # Store as JSON string
        )

        db.add(credential)
        db.commit()
        db.refresh(credential)

        logger.info(
            f"Stored credential {credential.id} for user {user_id}, provider {provider}"
        )
        return credential

    def get_decrypted_credential(
        self, db: Session, credential_id: str, user_id: int
    ) -> str | None:
        """
        Retrieve and decrypt a credential token.

        Args:
            db: Database session
            credential_id: UUID of the credential
            user_id: User ID for authorization check

        Returns:
            Decrypted plaintext token or None if not found/unauthorized
        """
        stmt = select(CloudProviderCredential).where(
            CloudProviderCredential.id == credential_id,
            CloudProviderCredential.user_id == user_id,
        )
        credential = db.scalar(stmt)

        if not credential:
            logger.warning(
                f"Credential {credential_id} not found or unauthorized for user {user_id}"
            )
            return None

        return self.decrypt_token(credential.encrypted_token)

    def rotate_credential(
        self, db: Session, credential_id: str, user_id: int, new_plaintext_token: str
    ) -> bool:
        """
        Rotate an existing credential with a new token.

        Args:
            db: Database session
            credential_id: UUID of the credential
            user_id: User ID for authorization
            new_plaintext_token: New token to encrypt

        Returns:
            True if rotation succeeded, False otherwise
        """
        stmt = select(CloudProviderCredential).where(
            CloudProviderCredential.id == credential_id,
            CloudProviderCredential.user_id == user_id,
        )
        credential = db.scalar(stmt)

        if not credential:
            logger.warning(
                f"Credential {credential_id} not found for rotation by user {user_id}"
            )
            return False

        credential.encrypted_token = self.encrypt_token(new_plaintext_token)
        credential.token_hash = self.hash_token(new_plaintext_token)

        db.commit()
        logger.info(f"Rotated credential {credential_id} for user {user_id}")
        return True

    def delete_credential(self, db: Session, credential_id: str, user_id: int) -> bool:
        """
        Delete a credential.

        Args:
            db: Database session
            credential_id: UUID of the credential
            user_id: User ID for authorization

        Returns:
            True if deleted, False if not found
        """
        stmt = select(CloudProviderCredential).where(
            CloudProviderCredential.id == credential_id,
            CloudProviderCredential.user_id == user_id,
        )
        credential = db.scalar(stmt)

        if not credential:
            logger.warning(
                f"Credential {credential_id} not found for deletion by user {user_id}"
            )
            return False

        db.delete(credential)
        db.commit()
        logger.info(f"Deleted credential {credential_id} for user {user_id}")
        return True
