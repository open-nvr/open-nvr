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

"""The MediaMTX signing key pair survives its own first start.

The first boot of a fresh install used to race itself: MediaMTX fetched the
JWKS while the first stream token was minted, both threads saw no key files,
both generated a pair, and the two unlocked writes interleaved — the public
file belonged to a private key that no longer existed, so every stream token
was rejected until someone rewrote it by hand. These tests pin the three
properties that stop that: creation is serialised, writes are atomic, and a
pair that is already torn is repaired from the private key on load.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from services.mediamtx_jwt_service import MediaMtxJwtService as Svc


def _reset_class_state() -> None:
    Svc._private_key = None
    Svc._public_key = None
    Svc._kid = None


@pytest.fixture
def keys_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the service at an empty temp dir and clear its in-memory keys."""
    d = tmp_path / "keys"
    monkeypatch.setattr(Svc, "KEYS_DIR", d)
    monkeypatch.setattr(Svc, "PRIVATE_KEY_PATH", d / "mediamtx_private.pem")
    monkeypatch.setattr(Svc, "PUBLIC_KEY_PATH", d / "mediamtx_public.pem")
    _reset_class_state()
    yield d
    _reset_class_state()


def _load_private(path: Path) -> rsa.RSAPrivateKey:
    key = serialization.load_pem_private_key(
        path.read_bytes(), password=None, backend=default_backend()
    )
    assert isinstance(key, rsa.RSAPrivateKey)
    return key


def _load_public(path: Path) -> rsa.RSAPublicKey:
    key = serialization.load_pem_public_key(
        path.read_bytes(), backend=default_backend()
    )
    assert isinstance(key, rsa.RSAPublicKey)
    return key


def _pair_matches(keys_dir: Path) -> bool:
    priv = _load_private(keys_dir / "mediamtx_private.pem")
    pub = _load_public(keys_dir / "mediamtx_public.pem")
    return pub.public_numbers() == priv.public_key().public_numbers()


def _fresh_pair() -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    priv = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return priv, pub


def test_first_start_creates_a_matching_pair(keys_dir: Path) -> None:
    Svc._load_keys()
    assert _pair_matches(keys_dir)
    assert (
        Svc._kid
        == hashlib.sha256((keys_dir / "mediamtx_public.pem").read_bytes()).hexdigest()[
            :16
        ]
    )
    # The in-memory private key is the one on disk.
    assert Svc._private_key is not None
    assert (
        Svc._private_key.private_numbers()
        == _load_private(keys_dir / "mediamtx_private.pem").private_numbers()
    )


def test_concurrent_first_start_yields_one_consistent_pair(keys_dir: Path) -> None:
    """Sixteen threads race the first load (JWKS fetch vs. token mint)."""
    n = 16
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=10)
            Svc._load_keys()
        except BaseException as exc:  # pragma: no cover - surfaced by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors
    assert _pair_matches(keys_dir)
    assert Svc._public_key is not None and Svc._private_key is not None
    assert (
        Svc._public_key.public_numbers()
        == Svc._private_key.public_key().public_numbers()
    )


def test_torn_pair_is_repaired_from_the_private_key(
    keys_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A public file from another generation (the old race) is rewritten."""
    keys_dir.mkdir()
    priv_a, _ = _fresh_pair()
    _, pub_b = _fresh_pair()
    (keys_dir / "mediamtx_private.pem").write_bytes(priv_a)
    (keys_dir / "mediamtx_public.pem").write_bytes(pub_b)
    assert not _pair_matches(keys_dir)

    Svc._load_keys()

    assert _pair_matches(keys_dir)
    assert (keys_dir / "mediamtx_private.pem").read_bytes() == priv_a, (
        "the private key is kept"
    )
    jwk = Svc.get_jwks()["keys"][0]
    assert jwk["kid"] == Svc._kid
    assert (
        Svc._kid
        == hashlib.sha256((keys_dir / "mediamtx_public.pem").read_bytes()).hexdigest()[
            :16
        ]
    )


def test_corrupt_public_file_is_repaired(keys_dir: Path) -> None:
    keys_dir.mkdir()
    priv_a, _ = _fresh_pair()
    (keys_dir / "mediamtx_private.pem").write_bytes(priv_a)
    (keys_dir / "mediamtx_public.pem").write_bytes(
        b"-----BEGIN PUBLIC KEY-----\nnot a key\n-----END PUBLIC KEY-----\n"
    )

    Svc._load_keys()

    assert _pair_matches(keys_dir)
    assert (keys_dir / "mediamtx_private.pem").read_bytes() == priv_a


def test_missing_public_half_is_derived_not_regenerated(keys_dir: Path) -> None:
    """A start interrupted between the two writes keeps its private key."""
    keys_dir.mkdir()
    priv_a, pub_a = _fresh_pair()
    (keys_dir / "mediamtx_private.pem").write_bytes(priv_a)

    Svc._load_keys()

    assert (keys_dir / "mediamtx_private.pem").read_bytes() == priv_a
    assert (keys_dir / "mediamtx_public.pem").read_bytes() == pub_a


def test_unreadable_private_key_is_replaced(keys_dir: Path) -> None:
    keys_dir.mkdir()
    (keys_dir / "mediamtx_private.pem").write_bytes(b"garbage")

    Svc._load_keys()

    assert _pair_matches(keys_dir)


def test_no_temp_files_left_behind(keys_dir: Path) -> None:
    Svc._load_keys()
    names = sorted(p.name for p in keys_dir.iterdir())
    assert names == sorted(
        [Svc.LOCK_FILE_NAME, "mediamtx_private.pem", "mediamtx_public.pem"]
    )


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX permission bits")
def test_private_key_is_owner_only_from_the_first_byte(keys_dir: Path) -> None:
    Svc._load_keys()
    mode = (keys_dir / "mediamtx_private.pem").stat().st_mode & 0o777
    assert mode == 0o600


def test_load_is_idempotent_once_loaded(keys_dir: Path) -> None:
    Svc._load_keys()
    first = Svc._private_key
    # Even if the files vanish, an already-loaded process keeps its keys.
    (keys_dir / "mediamtx_public.pem").unlink()
    Svc._load_keys()
    assert Svc._private_key is first
