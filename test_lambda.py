"""
test_lambda.py
==============
pytest + moto unit and integration tests for the S3 SCAP XML Collector Lambda.

Run with:
    pytest test_lambda.py -v

Dependencies:
    pip install pytest moto[s3] boto3
"""

from __future__ import annotations

import io
import os
import re
import zipfile
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

# ---------------------------------------------------------------------------
# Constants mirroring the Lambda defaults
# ---------------------------------------------------------------------------
SOURCE_BUCKET = "test-source-bucket"
DEST_BUCKET = "test-dest-bucket"
BASE_PREFIX = "reports/test/"
DATE_FOLDER = "2024-01-15"
DATE_PREFIX = f"{BASE_PREFIX}{DATE_FOLDER}/"

IP_1 = "192.168.1.100"
IP_2 = "192.168.1.101"

XML_KEY_1 = f"{DATE_PREFIX}{IP_1}/Results/SCAP/XML/scan_result_1.xml"
XML_KEY_2 = f"{DATE_PREFIX}{IP_2}/Results/SCAP/XML/scan_result_2.xml"
XML_KEY_3 = f"{DATE_PREFIX}{IP_2}/Results/SCAP/XML/scan_result_3.xml"

FAKE_XML_CONTENT = b'<?xml version="1.0"?><TestResult><host>%b</host></TestResult>'


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set dummy AWS credentials so moto doesn't try to use real ones."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture(autouse=True)
def env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject required Lambda environment variables."""
    monkeypatch.setenv("SOURCE_BUCKET", SOURCE_BUCKET)
    monkeypatch.setenv("DESTINATION_BUCKET", DEST_BUCKET)
    monkeypatch.setenv("BASE_PREFIX", BASE_PREFIX)
    monkeypatch.setenv("MAX_ZIP_SIZE_MB", "450")


@pytest.fixture
def mock_context() -> MagicMock:
    """Return a minimal mock Lambda context object."""
    ctx = MagicMock()
    ctx.function_name = "s3-scap-xml-collector-test"
    ctx.aws_request_id = "test-request-id"
    return ctx


def _create_buckets(s3_client: boto3.client) -> None:
    """Create the source and destination S3 buckets."""
    s3_client.create_bucket(Bucket=SOURCE_BUCKET)
    s3_client.create_bucket(Bucket=DEST_BUCKET)


def _populate_source_bucket(s3_client: boto3.client) -> None:
    """
    Upload three fake XML files across two IP folders in the source bucket.

    Structure:
      reports/test/2024-01-15/192.168.1.100/Results/SCAP/XML/scan_result_1.xml
      reports/test/2024-01-15/192.168.1.101/Results/SCAP/XML/scan_result_2.xml
      reports/test/2024-01-15/192.168.1.101/Results/SCAP/XML/scan_result_3.xml
    """
    for key, ip in [
        (XML_KEY_1, IP_1.encode()),
        (XML_KEY_2, IP_2.encode()),
        (XML_KEY_3, IP_2.encode()),
    ]:
        s3_client.put_object(
            Bucket=SOURCE_BUCKET,
            Key=key,
            Body=FAKE_XML_CONTENT % ip,
        )


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


@mock_aws
def test_successful_collection(mock_context: MagicMock) -> None:
    """
    Happy path: 3 XML files across 2 IPs should produce exactly 1 ZIP in
    the destination bucket.
    """
    from lambda_function import lambda_handler

    s3 = boto3.client("s3", region_name="us-east-1")
    _create_buckets(s3)
    _populate_source_bucket(s3)

    event: dict = {"source": "aws.events", "detail-type": "Scheduled Event", "detail": {}}
    result = lambda_handler(event, mock_context)

    assert result["statusCode"] == 200

    # Verify exactly one object was written to the destination bucket
    response = s3.list_objects_v2(Bucket=DEST_BUCKET, Prefix="scap-results/")
    objects = response.get("Contents", [])
    assert len(objects) == 1, f"Expected 1 ZIP, found {len(objects)}"


@mock_aws
def test_zip_contains_correct_files(mock_context: MagicMock) -> None:
    """
    The ZIP archive uploaded to the destination bucket must contain exactly
    3 XML entries, one per source XML file.
    """
    from lambda_function import lambda_handler

    s3 = boto3.client("s3", region_name="us-east-1")
    _create_buckets(s3)
    _populate_source_bucket(s3)

    event: dict = {"source": "aws.events", "detail-type": "Scheduled Event", "detail": {}}
    lambda_handler(event, mock_context)

    # Download the ZIP
    response = s3.list_objects_v2(Bucket=DEST_BUCKET, Prefix="scap-results/")
    zip_key = response["Contents"][0]["Key"]
    zip_obj = s3.get_object(Bucket=DEST_BUCKET, Key=zip_key)
    zip_bytes = zip_obj["Body"].read()

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()

    assert len(names) == 3, f"Expected 3 entries in ZIP, got {len(names)}: {names}"

    # Each entry must follow the "<IP>/<filename>.xml" pattern
    for name in names:
        parts = name.split("/")
        assert len(parts) == 2, f"Unexpected archive entry path: {name}"
        assert parts[1].endswith(".xml"), f"Non-XML entry in archive: {name}"


@mock_aws
def test_zip_key_matches_expected_pattern(mock_context: MagicMock) -> None:
    """
    The ZIP object key in the destination bucket must match the pattern:
      scap-results/<DATE>/scap_results_<DATE>_<TIMESTAMP>.zip
    """
    from lambda_function import lambda_handler

    s3 = boto3.client("s3", region_name="us-east-1")
    _create_buckets(s3)
    _populate_source_bucket(s3)

    event: dict = {"source": "aws.events", "detail-type": "Scheduled Event", "detail": {}}
    lambda_handler(event, mock_context)

    response = s3.list_objects_v2(Bucket=DEST_BUCKET, Prefix="scap-results/")
    zip_key = response["Contents"][0]["Key"]

    pattern = re.compile(
        r"^scap-results/"
        r"(?P<date>\d{4}-\d{2}-\d{2})/"
        r"scap_results_(?P=date)_\d{8}T\d{6}Z\.zip$"
    )
    assert pattern.match(zip_key), (
        f"ZIP key '{zip_key}' does not match expected pattern."
    )


@mock_aws
def test_zip_preserves_ip_folder_structure(mock_context: MagicMock) -> None:
    """
    Entries inside the ZIP must be prefixed with the originating IP address,
    e.g. '192.168.1.100/scan_result_1.xml'.
    """
    from lambda_function import lambda_handler

    s3 = boto3.client("s3", region_name="us-east-1")
    _create_buckets(s3)
    _populate_source_bucket(s3)

    event: dict = {"source": "aws.events", "detail-type": "Scheduled Event", "detail": {}}
    lambda_handler(event, mock_context)

    response = s3.list_objects_v2(Bucket=DEST_BUCKET, Prefix="scap-results/")
    zip_key = response["Contents"][0]["Key"]
    zip_bytes = s3.get_object(Bucket=DEST_BUCKET, Key=zip_key)["Body"].read()

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = set(zf.namelist())

    assert f"{IP_1}/scan_result_1.xml" in names
    assert f"{IP_2}/scan_result_2.xml" in names
    assert f"{IP_2}/scan_result_3.xml" in names


@mock_aws
def test_s3_event_trigger(mock_context: MagicMock) -> None:
    """
    The handler should behave identically when triggered by an S3 event.
    """
    from lambda_function import lambda_handler

    s3 = boto3.client("s3", region_name="us-east-1")
    _create_buckets(s3)
    _populate_source_bucket(s3)

    # Simulate an S3-event-style invocation payload
    s3_event: dict = {
        "Records": [
            {
                "eventSource": "aws:s3",
                "eventName": "ObjectCreated:Put",
                "s3": {
                    "bucket": {"name": SOURCE_BUCKET},
                    "object": {"key": XML_KEY_1},
                },
            }
        ]
    }
    result = lambda_handler(s3_event, mock_context)
    assert result["statusCode"] == 200

    # Confirm a ZIP was created
    response = s3.list_objects_v2(Bucket=DEST_BUCKET, Prefix="scap-results/")
    assert len(response.get("Contents", [])) == 1


# ---------------------------------------------------------------------------
# Edge-case: no date folders
# ---------------------------------------------------------------------------


@mock_aws
def test_no_date_folders_raises(mock_context: MagicMock) -> None:
    """
    If there are no date folders under BASE_PREFIX, lambda_handler must raise
    a ValueError with a descriptive message (so Lambda marks the invocation
    as failed).
    """
    from lambda_function import lambda_handler

    s3 = boto3.client("s3", region_name="us-east-1")
    _create_buckets(s3)
    # Intentionally leave the source bucket empty — no date folders exist.

    event: dict = {"source": "aws.events", "detail-type": "Scheduled Event", "detail": {}}

    with pytest.raises(ValueError, match="No date folders found"):
        lambda_handler(event, mock_context)


# ---------------------------------------------------------------------------
# Edge-case: no XML files
# ---------------------------------------------------------------------------


@mock_aws
def test_no_xml_files_returns_gracefully(mock_context: MagicMock) -> None:
    """
    If date and IP folders exist but no XML files are found, the Lambda should
    return a 200 response with a warning message instead of raising an
    exception.
    """
    from lambda_function import lambda_handler

    s3 = boto3.client("s3", region_name="us-east-1")
    _create_buckets(s3)

    # Create the date and IP folder structure but upload a non-XML file only
    non_xml_key = f"{DATE_PREFIX}{IP_1}/Results/SCAP/XML/notes.txt"
    s3.put_object(Bucket=SOURCE_BUCKET, Key=non_xml_key, Body=b"not an xml file")

    event: dict = {"source": "aws.events", "detail-type": "Scheduled Event", "detail": {}}
    result = lambda_handler(event, mock_context)

    assert result["statusCode"] == 200
    assert "No XML files" in result["body"] or "Nothing to zip" in result["body"]

    # Confirm no ZIP was created
    response = s3.list_objects_v2(Bucket=DEST_BUCKET, Prefix="scap-results/")
    assert response.get("Contents") is None or len(response.get("Contents", [])) == 0


# ---------------------------------------------------------------------------
# Unit tests for individual helper functions
# ---------------------------------------------------------------------------


@mock_aws
def test_get_latest_date_folder_returns_most_recent() -> None:
    """
    get_latest_date_folder should return the lexicographically greatest date
    folder when multiple exist.
    """
    from lambda_function import get_latest_date_folder

    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=SOURCE_BUCKET)

    # Create objects under three different date folders
    for date in ["2023-12-01", "2024-01-15", "2024-03-07"]:
        s3.put_object(
            Bucket=SOURCE_BUCKET,
            Key=f"{BASE_PREFIX}{date}/{IP_1}/Results/SCAP/XML/scan.xml",
            Body=b"<xml/>",
        )

    latest = get_latest_date_folder(s3, SOURCE_BUCKET, BASE_PREFIX)
    assert latest == f"{BASE_PREFIX}2024-03-07/"


@mock_aws
def test_get_latest_date_folder_empty_raises() -> None:
    """
    get_latest_date_folder must raise ValueError when the base prefix is empty.
    """
    from lambda_function import get_latest_date_folder

    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=SOURCE_BUCKET)

    with pytest.raises(ValueError, match="No date folders found"):
        get_latest_date_folder(s3, SOURCE_BUCKET, BASE_PREFIX)


@mock_aws
def test_find_xml_files_ignores_non_xml() -> None:
    """
    find_xml_files must not return keys that do not end in '.xml'.
    """
    from lambda_function import find_xml_files

    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=SOURCE_BUCKET)

    xml_prefix = f"{DATE_PREFIX}{IP_1}/Results/SCAP/XML/"
    s3.put_object(Bucket=SOURCE_BUCKET, Key=f"{xml_prefix}scan.xml", Body=b"<xml/>")
    s3.put_object(Bucket=SOURCE_BUCKET, Key=f"{xml_prefix}notes.txt", Body=b"notes")
    s3.put_object(Bucket=SOURCE_BUCKET, Key=f"{xml_prefix}report.html", Body=b"<html/>")

    ip_prefix = f"{DATE_PREFIX}{IP_1}/"
    keys = find_xml_files(s3, SOURCE_BUCKET, ip_prefix)

    assert keys == [f"{xml_prefix}scan.xml"]


@mock_aws
def test_download_and_zip_files_produces_valid_zip() -> None:
    """
    download_and_zip_files must return a valid, non-empty ZIP buffer.
    """
    from lambda_function import download_and_zip_files

    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=SOURCE_BUCKET)
    _populate_source_bucket(s3)

    xml_keys = [XML_KEY_1, XML_KEY_2, XML_KEY_3]
    buf, count = download_and_zip_files(s3, SOURCE_BUCKET, xml_keys)

    assert count == 3
    assert buf.tell() == 0  # buffer should be seeked to 0

    with zipfile.ZipFile(buf) as zf:
        assert len(zf.namelist()) == 3
