"""
The Zero-Save Policy. CON-001, DR-001, FR-CAM-004.

Three checks, each against the strongest form of the guarantee available:

  * The schema has no column capable of holding an image -- checked against
    `Base.metadata`, so a binary column added to ANY model fails here, not
    only one added to ScanLog.
  * The in-memory image type cannot print its bytes into a log line.
  * A full scan, including a failing one, leaves no image bytes in the log
    output and no file on disk.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

from sqlalchemy import LargeBinary

from app.db.models import Base
from app.services.analysis.base import PreparedImage
from tests.test_scan import JPEG, StubProvider, clear, onboard, scan, use_pipeline, with_signal


def test_no_table_has_a_binary_column():
    offenders = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, LargeBinary)
    ]
    assert offenders == []


# Named exceptions, each a short text column that cannot hold image bytes.
# products.image_url is the address of the brand's PUBLIC product photo on its
# own store (ADR-030): catalogue data, never a user's photograph. Adding to
# this list needs the same justification, not a quieter name.
ALLOWED_IMAGE_NAMED = {"products.image_url"}


def assert_allowed_is_a_short_url(table, column) -> None:
    from sqlalchemy import String

    assert isinstance(column.type, String) and column.type.length and column.type.length <= 1000, (
        f"{table.name}.{column.name} is allowed only as a short URL column")


def test_no_column_is_named_for_an_image():
    """SRS 6.2: `imagePayload` must not exist as a persisted column."""
    suspicious = ("image", "photo", "payload", "picture", "selfie")
    offenders = []
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if not any(word in column.name.lower() for word in suspicious):
                continue
            if f"{table.name}.{column.name}" in ALLOWED_IMAGE_NAMED:
                assert_allowed_is_a_short_url(table, column)
                continue
            offenders.append(f"{table.name}.{column.name}")
    assert offenders == []


def test_prepared_image_repr_carries_no_bytes():
    image = PreparedImage(data=b"\xff\xd8secret-face-bytes")
    assert "secret-face-bytes" not in repr(image)
    assert "secret-face-bytes" not in str(image)


async def _scan_and_capture(client, settings, caplog, payload) -> str:
    use_pipeline(settings, StubProvider(payload))
    headers = await onboard(client)
    with caplog.at_level(logging.DEBUG):
        await scan(client, headers)
    return caplog.text


async def test_scan_logs_contain_no_image_data(client, settings, caplog):
    text = await _scan_and_capture(client, settings, caplog, with_signal())
    assert "not-really-a-photo" not in text
    assert base64.b64encode(JPEG).decode()[:24] not in text


async def test_failed_scan_logs_contain_no_image_data(client, settings, caplog):
    """FR-CAM-004: 'Given an unhandled exception, the log entry contains no image data.'"""
    text = await _scan_and_capture(client, settings, caplog, {"cosmeticConcerns": 7})
    assert "not-really-a-photo" not in text


async def test_scan_writes_no_file(client, settings, tmp_path, monkeypatch):
    """
    Runs a scan from an empty working directory and checks it is still empty.
    Any relative-path write -- a debug dump, a temp upload -- lands here.
    """
    use_pipeline(settings, StubProvider(clear()))
    headers = await onboard(client)
    monkeypatch.chdir(tmp_path)

    await scan(client, headers)

    assert list(Path(tmp_path).rglob("*")) == []
