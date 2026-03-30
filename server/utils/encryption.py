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

from cryptography.fernet import Fernet

from core.config import settings


class EncryptionManager:
    """
    Manages encryption and decryption of sensitive data using Fernet (symmetric encryption).
    Uses the CREDENTIAL_ENCRYPTION_KEY from settings.
    """

    def __init__(self):
        key = settings.credential_encryption_key
        if not key:
            raise ValueError("CREDENTIAL_ENCRYPTION_KEY is not set in configuration")

        # Ensure key is bytes
        if isinstance(key, str):
            key = key.encode()

        try:
            self.cipher = Fernet(key)
        except Exception as e:
            raise ValueError(f"Invalid CREDENTIAL_ENCRYPTION_KEY: {e}")

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a plaintext string."""
        if not plaintext:
            return ""
        try:
            token = self.cipher.encrypt(plaintext.encode())
            return token.decode()
        except Exception as e:
            print(f"Encryption error: {e}")
            raise

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt a ciphertext string."""
        if not ciphertext:
            return ""
        try:
            plaintext = self.cipher.decrypt(ciphertext.encode())
            return plaintext.decode()
        except Exception as e:
            print(f"Decryption error: {e}")
            raise


# Global instance
_manager: EncryptionManager | None = None


def get_encryption_manager() -> EncryptionManager:
    global _manager
    if _manager is None:
        _manager = EncryptionManager()
    return _manager


def encrypt_value(value: str) -> str:
    """Helper to encrypt a value using the global manager."""
    return get_encryption_manager().encrypt(value)


def decrypt_value(value: str) -> str:
    """Helper to decrypt a value using the global manager."""
    return get_encryption_manager().decrypt(value)
