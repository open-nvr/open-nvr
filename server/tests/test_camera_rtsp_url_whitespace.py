import os
import secrets
import sys

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))

from schemas import CameraCreate, CameraUpdate


def _create(**kwargs):
    return CameraCreate(name="c", ip_address="10.0.0.5", **kwargs)


def test_create_strips_rtsp_and_substream_urls():
    camera = _create(
        rtsp_url="  rtsp://u:p@10.0.0.5:554/s  ",
        substream_url="\trtsp://10.0.0.5/sub\n",
    )
    assert camera.rtsp_url == "rtsp://u:p@10.0.0.5:554/s"
    assert camera.substream_url == "rtsp://10.0.0.5/sub"


def test_create_whitespace_only_url_is_empty():
    camera = _create(rtsp_url="   ", substream_url="\n")
    assert not camera.rtsp_url
    assert not camera.substream_url


def test_update_strips_rtsp_and_substream_urls():
    update = CameraUpdate(
        rtsp_url=" rtsp://10.0.0.5/s", substream_url="rtsp://10.0.0.5/sub\r\n"
    )
    assert update.rtsp_url == "rtsp://10.0.0.5/s"
    assert update.substream_url == "rtsp://10.0.0.5/sub"


def test_update_whitespace_only_url_stays_empty_string():
    assert CameraUpdate(rtsp_url="  ").rtsp_url == ""
    assert CameraUpdate(rtsp_url=None).rtsp_url is None


@pytest.mark.parametrize("url", ["  ftp://10.0.0.5/s", " rtsp://10.0.0.5/$(id)"])
def test_create_still_rejects_bad_urls_after_stripping(url):
    with pytest.raises(ValidationError):
        _create(rtsp_url=url)
