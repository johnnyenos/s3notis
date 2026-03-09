"""
S3 SCAP XML Collector Lambda
============================
Collects SCAP XML files from a source S3 bucket, zips them in memory,
and uploads the resulting archive to a destination S3 bucket.

Supports two trigger types:
  - S3 Event (s3:ObjectCreated:*) on the source bucket
  - EventBridge scheduled rule (monthly cron)

Environment Variables:
  SOURCE_BUCKET       : Name of the source S3 bucket (required)
  DESTINATION_BUCKET  : Name of the destination S3 bucket (required)
  BASE_PREFIX         : Key prefix to scan under (default: "reports/test/")
  MAX_ZIP_SIZE_MB     : Soft warning threshold in MB (default: 450)
  SNS_TOPIC_ARN       : (optional) ARN of an SNS topic for notifications
"""

from __future__ import annotations

import io
import logging
import os
import zipfile
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import boto3
import botocore.exceptions

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------
DEFAULT_BASE_PREFIX = "reports/test/"
DEFAULT_MAX_ZIP_SIZE_MB = 450
CONTENT_TYPE_ZIP = "application/zip"


# ---------------------------------------------------------------------------
# Helper: SNS notification scaffold (future iteration)
# ---------------------------------------------------------------------------
def send_sns_notification(subject: str, message: str, success: bool = True) -> None:
    """
    Publish a notification to an SNS topic.

    The topic ARN is read from the ``SNS_TOPIC_ARN`` environment variable.
    If the variable is not set the function returns silently so that missing
    SNS configuration never breaks the main Lambda flow.

    Args:
        subject: Short subject line for the notification.
        message: Full notification body.
        success: ``True`` for informational messages, ``False`` for alerts.
    """
    topic_arn = os.environ.get("SNS_TOPIC_ARN", "")
    if not topic_arn:
        logger.debug("SNS_TOPIC_ARN not set — skipping SNS notification.")
        return

    # sns_client = boto3.client("sns")
    # try:
    #     # TODO: Uncomment when SNS topic is ready
    #     # sns_client.publish(
    #     #     TopicArn=topic_arn,
    #     #     Subject=subject,
    #     #     Message=message,
    #     #     MessageAttributes={
    #     #         "status": {
    #     #             "DataType": "String",
    #     #             "StringValue": "success" if success else "failure",
    #     #         }
    #     #     },
    #     # )
    #     # logger.info("SNS notification sent: %s", subject)
    #     pass
    # except botocore.exceptions.ClientError:
    #     logger.exception("Failed to publish SNS notification.")


# ---------------------------------------------------------------------------
# Core helper functions
# ---------------------------------------------------------------------------


def get_latest_date_folder(s3_client: "S3Client", bucket: str, base_prefix: str) -> str:
    """
    Return the key prefix for the most-recently-created date folder under
    ``base_prefix``.

    Date folders are expected to follow ISO-8601 naming conventions
    (``YYYY-MM-DD`` or ``YYYYMMDD``).  Lexicographic sort is correct for both
    formats as long as zero-padding is consistent, which is guaranteed by both
    styles.

    Args:
        s3_client:   An initialised boto3 S3 client.
        bucket:      Source bucket name.
        base_prefix: The prefix directly above the date folders, e.g.
                     ``"reports/test/"``.

    Returns:
        The full key prefix of the latest date folder including trailing slash,
        e.g. ``"reports/test/2024-01-15/"``.

    Raises:
        ValueError: If no date folders are found under ``base_prefix``.
        botocore.exceptions.ClientError: On unexpected S3 API errors.
    """
    logger.info("Listing date folders under s3://%s/%s", bucket, base_prefix)

    paginator = s3_client.get_paginator("list_objects_v2")
    date_prefixes: list[str] = []

    try:
        for page in paginator.paginate(Bucket=bucket, Prefix=base_prefix, Delimiter="/"):
            for cp in page.get("CommonPrefixes", []):
                date_prefixes.append(cp["Prefix"])
    except botocore.exceptions.ClientError:
        logger.exception("Failed to list date folders in bucket %s", bucket)
        raise

    if not date_prefixes:
        raise ValueError(
            f"No date folders found under s3://{bucket}/{base_prefix}. "
            "Verify the bucket name and BASE_PREFIX environment variable."
        )

    # Sort lexicographically — correct for ISO-8601 dates with consistent padding.
    date_prefixes.sort()
    latest = date_prefixes[-1]
    logger.info("Latest date folder: %s (%d total found)", latest, len(date_prefixes))
    return latest


def list_ip_folders(s3_client: "S3Client", bucket: str, date_prefix: str) -> list[str]:
    """
    Return a list of key prefixes representing IP-address sub-folders directly
    under ``date_prefix``.

    Args:
        s3_client:   An initialised boto3 S3 client.
        bucket:      Source bucket name.
        date_prefix: Full key prefix of the date folder, e.g.
                     ``"reports/test/2024-01-15/"``.

    Returns:
        A list of key prefixes, one per IP address folder, e.g.
        ``["reports/test/2024-01-15/192.168.1.1/", ...]``.
        Returns an empty list if no sub-folders exist.

    Raises:
        botocore.exceptions.ClientError: On unexpected S3 API errors.
    """
    logger.info("Listing IP folders under s3://%s/%s", bucket, date_prefix)

    paginator = s3_client.get_paginator("list_objects_v2")
    ip_prefixes: list[str] = []

    try:
        for page in paginator.paginate(Bucket=bucket, Prefix=date_prefix, Delimiter="/"):
            for cp in page.get("CommonPrefixes", []):
                ip_prefixes.append(cp["Prefix"])
    except botocore.exceptions.ClientError:
        logger.exception("Failed to list IP folders under %s", date_prefix)
        raise

    logger.info("Found %d IP address folder(s).", len(ip_prefixes))
    return ip_prefixes


def find_xml_files(s3_client: "S3Client", bucket: str, ip_prefix: str) -> list[str]:
    """
    Return all ``.xml`` object keys nested under
    ``<ip_prefix>Results/SCAP/XML/``.

    Args:
        s3_client: An initialised boto3 S3 client.
        bucket:    Source bucket name.
        ip_prefix: Full key prefix of the IP-address folder, e.g.
                   ``"reports/test/2024-01-15/192.168.1.1/"``.

    Returns:
        A (possibly empty) list of S3 object keys for XML files.

    Raises:
        botocore.exceptions.ClientError: On unexpected S3 API errors.
    """
    xml_search_prefix = f"{ip_prefix}Results/SCAP/XML/"
    logger.debug("Searching for XML files under s3://%s/%s", bucket, xml_search_prefix)

    paginator = s3_client.get_paginator("list_objects_v2")
    xml_keys: list[str] = []

    try:
        for page in paginator.paginate(Bucket=bucket, Prefix=xml_search_prefix):
            for obj in page.get("Contents", []):
                key: str = obj["Key"]
                if key.endswith(".xml"):
                    xml_keys.append(key)
    except botocore.exceptions.ClientError:
        logger.exception("Failed to list XML files under %s", xml_search_prefix)
        raise

    if not xml_keys:
        logger.warning("No XML files found under s3://%s/%s", bucket, xml_search_prefix)

    logger.info("Found %d XML file(s) under %s", len(xml_keys), ip_prefix)
    return xml_keys


def download_and_zip_files(
    s3_client: "S3Client",
    bucket: str,
    xml_keys: list[str],
) -> tuple[io.BytesIO, int]:
    """
    Download every object in ``xml_keys`` from ``bucket`` and pack them into
    an in-memory ZIP archive.

    Each file inside the ZIP is stored at ``<IP_ADDRESS>/<filename>.xml`` so
    that the originating host is preserved.

    Args:
        s3_client: An initialised boto3 S3 client.
        bucket:    Source bucket name.
        xml_keys:  List of S3 object keys to include in the archive.

    Returns:
        A 2-tuple of:
          - ``io.BytesIO`` — seeked to position 0, ready for upload.
          - ``int`` — total number of files successfully added to the archive.

    Raises:
        botocore.exceptions.ClientError: If any individual download fails.
    """
    zip_buffer = io.BytesIO()
    files_added = 0

    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for key in xml_keys:
            # key example: reports/test/2024-01-15/192.168.1.100/Results/SCAP/XML/scan.xml
            parts = key.split("/")
            # IP address is the 4th segment (index 3) in the standard path structure.
            # Gracefully handle unexpected path depths.
            if len(parts) >= 5:
                ip_address = parts[3]
                filename = parts[-1]
            else:
                ip_address = "unknown"
                filename = parts[-1]

            archive_name = f"{ip_address}/{filename}"
            logger.debug("Downloading s3://%s/%s → %s", bucket, key, archive_name)

            try:
                response = s3_client.get_object(Bucket=bucket, Key=key)
                file_data = response["Body"].read()
            except botocore.exceptions.ClientError:
                logger.exception("Failed to download s3://%s/%s — skipping.", bucket, key)
                continue

            zf.writestr(archive_name, file_data)
            files_added += 1

    zip_buffer.seek(0)
    return zip_buffer, files_added


def upload_zip(
    s3_client: "S3Client",
    bucket: str,
    zip_buffer: io.BytesIO,
    destination_key: str,
) -> None:
    """
    Upload an in-memory ZIP archive to S3.

    Args:
        s3_client:       An initialised boto3 S3 client.
        bucket:          Destination bucket name.
        zip_buffer:      ``io.BytesIO`` positioned at 0 containing the ZIP data.
        destination_key: S3 key under which to store the archive.

    Raises:
        botocore.exceptions.ClientError: If the upload fails.
    """
    logger.info("Uploading zip to s3://%s/%s", bucket, destination_key)

    zip_size_bytes = zip_buffer.seek(0, 2)
    zip_buffer.seek(0)
    zip_size_mb = zip_size_bytes / (1024 * 1024)

    max_zip_size_mb = int(os.environ.get("MAX_ZIP_SIZE_MB", DEFAULT_MAX_ZIP_SIZE_MB))
    if zip_size_mb > max_zip_size_mb:
        logger.warning(
            "ZIP size %.2f MB exceeds soft limit of %d MB. Consider splitting.",
            zip_size_mb,
            max_zip_size_mb,
        )
    else:
        logger.info("ZIP size: %.2f MB", zip_size_mb)

    try:
        s3_client.put_object(
            Bucket=bucket,
            Key=destination_key,
            Body=zip_buffer,
            ContentType=CONTENT_TYPE_ZIP,
        )
    except botocore.exceptions.ClientError:
        logger.exception("Failed to upload ZIP to s3://%s/%s", bucket, destination_key)
        raise

    logger.info("Successfully uploaded ZIP to s3://%s/%s", bucket, destination_key)


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------


def lambda_handler(event: dict, context: object) -> dict:
    """
    AWS Lambda entry point.

    Supports two invocation patterns:
      1. **S3 Event** — triggered by ``s3:ObjectCreated:*`` on the source bucket.
         The event payload is inspected but the function always processes the
         *latest* date folder regardless of which specific key was created.
      2. **EventBridge / CloudWatch** — scheduled invocation; event payload is
         used only for logging.

    Args:
        event:   The Lambda event payload (dict).
        context: The Lambda runtime context object.

    Returns:
        A dict with ``statusCode`` (int) and ``body`` (str).
    """
    logger.info("Lambda invocation started. Event source detected: %s", _detect_trigger(event))

    source_bucket = os.environ.get("SOURCE_BUCKET", "")
    destination_bucket = os.environ.get("DESTINATION_BUCKET", "")
    base_prefix = os.environ.get("BASE_PREFIX", DEFAULT_BASE_PREFIX)

    # Validate required env vars
    if not source_bucket:
        raise EnvironmentError("Environment variable SOURCE_BUCKET is not set.")
    if not destination_bucket:
        raise EnvironmentError("Environment variable DESTINATION_BUCKET is not set.")

    # Normalise base_prefix — must end with "/"
    if not base_prefix.endswith("/"):
        base_prefix = base_prefix + "/"

    s3_client: "S3Client" = boto3.client("s3")

    try:
        # Step 1 — find the latest date folder
        latest_date_prefix = get_latest_date_folder(s3_client, source_bucket, base_prefix)
        # Extract the bare date string for use in output paths, e.g. "2024-01-15"
        date_folder = latest_date_prefix.rstrip("/").split("/")[-1]

        # Step 2 — enumerate IP folders
        ip_prefixes = list_ip_folders(s3_client, source_bucket, latest_date_prefix)
        if not ip_prefixes:
            msg = f"No IP address folders found under {latest_date_prefix}."
            logger.warning(msg)
            send_sns_notification(
                subject="SCAP Collector — No IP Folders Found",
                message=msg,
                success=False,
            )
            return {"statusCode": 200, "body": msg}

        # Step 3 — find all XML files
        all_xml_keys: list[str] = []
        for ip_prefix in ip_prefixes:
            xml_keys = find_xml_files(s3_client, source_bucket, ip_prefix)
            all_xml_keys.extend(xml_keys)

        logger.info("Total XML files collected: %d", len(all_xml_keys))

        if not all_xml_keys:
            msg = (
                f"No XML files found in any IP folder under {latest_date_prefix}. "
                "Nothing to zip."
            )
            logger.warning(msg)
            send_sns_notification(
                subject="SCAP Collector — No XML Files Found",
                message=msg,
                success=False,
            )
            return {"statusCode": 200, "body": msg}

        # Step 4 — zip all XML files in memory
        zip_buffer, files_zipped = download_and_zip_files(
            s3_client, source_bucket, all_xml_keys
        )
        logger.info("Zipped %d file(s) successfully.", files_zipped)

        # Step 5 — upload the zip
        timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination_key = (
            f"scap-results/{date_folder}/scap_results_{date_folder}_{timestamp}.zip"
        )
        upload_zip(s3_client, destination_bucket, zip_buffer, destination_key)

        success_msg = (
            f"Uploaded {files_zipped} XML file(s) to "
            f"s3://{destination_bucket}/{destination_key}"
        )
        logger.info(success_msg)

        # SNS success notification (scaffold — see send_sns_notification)
        send_sns_notification(
            subject=f"SCAP Collector — Success ({date_folder})",
            message=success_msg,
            success=True,
        )

        return {"statusCode": 200, "body": success_msg}

    except Exception:
        logger.exception("Unhandled exception in lambda_handler.")
        send_sns_notification(
            subject="SCAP Collector — FAILED",
            message="An unhandled exception occurred. Check CloudWatch logs for details.",
            success=False,
        )
        raise


# ---------------------------------------------------------------------------
# Internal utility
# ---------------------------------------------------------------------------


def _detect_trigger(event: dict) -> str:
    """Return a human-readable label for the invocation trigger."""
    if "Records" in event:
        source = event["Records"][0].get("eventSource", "")
        if source == "aws:s3":
            return "S3 Event"
    if "source" in event and event.get("source") == "aws.events":
        return "EventBridge"
    return "Unknown / Direct Invocation"


# ---------------------------------------------------------------------------
# Local testing entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    # Optional: use a specific AWS named profile for local runs.
    aws_profile = os.environ.get("AWS_PROFILE")
    if aws_profile:
        boto3.setup_default_session(profile_name=aws_profile)
        logger.info("Using AWS profile: %s", aws_profile)

    # Mock Lambda event — simulates an EventBridge scheduled trigger.
    mock_event: dict = {
        "source": "aws.events",
        "detail-type": "Scheduled Event",
        "detail": {},
    }

    # Mock Lambda context — only the fields used by this function.
    class MockContext:
        function_name = "s3-scap-xml-collector"
        aws_request_id = "local-test-request-id"
        log_stream_name = "local-test"

    mock_context = MockContext()

    print("=" * 60)
    print("Running lambda_handler locally …")
    print("=" * 60)

    result = lambda_handler(mock_event, mock_context)
    print(json.dumps(result, indent=2))
