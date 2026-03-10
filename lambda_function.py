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

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

DEFAULT_BASE_PREFIX = "reports/test/"
DEFAULT_MAX_ZIP_SIZE_MB = 450
CONTENT_TYPE_ZIP = "application/zip"


def send_sns_notification(subject: str, message: str, success: bool = True) -> None:
    topic_arn = os.environ.get("SNS_TOPIC_ARN", "")
    if not topic_arn:
        logger.debug("SNS_TOPIC_ARN not set — skipping SNS notification.")
        return
    try:
        boto3.client("sns").publish(
            TopicArn=topic_arn,
            Subject=subject,
            Message=message,
            MessageAttributes={"status": {"DataType": "String", "StringValue": "success" if success else "failure"}},
        )
        logger.info("SNS notification sent: %s", subject)
    except botocore.exceptions.ClientError:
        logger.exception("Failed to publish SNS notification.")


def _list_common_prefixes(s3_client: "S3Client", bucket: str, prefix: str) -> list[str]:
    paginator = s3_client.get_paginator("list_objects_v2")
    try:
        return [
            cp["Prefix"]
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/")
            for cp in page.get("CommonPrefixes", [])
        ]
    except botocore.exceptions.ClientError:
        logger.exception("Failed to list prefixes under s3://%s/%s", bucket, prefix)
        raise


def get_latest_date_folder(s3_client: "S3Client", bucket: str, base_prefix: str) -> str:
    logger.info("Listing date folders under s3://%s/%s", bucket, base_prefix)
    prefixes = _list_common_prefixes(s3_client, bucket, base_prefix)
    if not prefixes:
        raise ValueError(
            f"No date folders found under s3://{bucket}/{base_prefix}. "
            "Verify the bucket name and BASE_PREFIX environment variable."
        )
    latest = sorted(prefixes)[-1]
    logger.info("Latest date folder: %s (%d total found)", latest, len(prefixes))
    return latest


def list_ip_folders(s3_client: "S3Client", bucket: str, date_prefix: str) -> list[str]:
    logger.info("Listing IP folders under s3://%s/%s", bucket, date_prefix)
    prefixes = _list_common_prefixes(s3_client, bucket, date_prefix)
    logger.info("Found %d IP address folder(s).", len(prefixes))
    return prefixes


def find_xml_files(s3_client: "S3Client", bucket: str, ip_prefix: str) -> list[str]:
    xml_search_prefix = f"{ip_prefix}Results/SCAP/XML/"
    paginator = s3_client.get_paginator("list_objects_v2")
    try:
        xml_keys = [
            obj["Key"]
            for page in paginator.paginate(Bucket=bucket, Prefix=xml_search_prefix)
            for obj in page.get("Contents", [])
            if obj["Key"].endswith(".xml")
        ]
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
    zip_buffer = io.BytesIO()
    files_added = 0
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for key in xml_keys:
            parts = key.split("/")
            ip_address = parts[3] if len(parts) >= 5 else "unknown"
            archive_name = f"{ip_address}/{parts[-1]}"
            try:
                file_data = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
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
    logger.info("Uploading zip to s3://%s/%s", bucket, destination_key)
    zip_size_mb = zip_buffer.seek(0, 2) / (1024 * 1024)
    zip_buffer.seek(0)
    max_zip_size_mb = int(os.environ.get("MAX_ZIP_SIZE_MB", DEFAULT_MAX_ZIP_SIZE_MB))
    if zip_size_mb > max_zip_size_mb:
        logger.warning("ZIP size %.2f MB exceeds soft limit of %d MB. Consider splitting.", zip_size_mb, max_zip_size_mb)
    else:
        logger.info("ZIP size: %.2f MB", zip_size_mb)
    try:
        s3_client.put_object(Bucket=bucket, Key=destination_key, Body=zip_buffer, ContentType=CONTENT_TYPE_ZIP)
    except botocore.exceptions.ClientError:
        logger.exception("Failed to upload ZIP to s3://%s/%s", bucket, destination_key)
        raise
    logger.info("Successfully uploaded ZIP to s3://%s/%s", bucket, destination_key)


def lambda_handler(event: dict, context: object) -> dict:
    logger.info("Lambda invocation started. Event source detected: %s", _detect_trigger(event))

    source_bucket = os.environ.get("SOURCE_BUCKET", "")
    destination_bucket = os.environ.get("DESTINATION_BUCKET", "")
    if not source_bucket:
        raise EnvironmentError("Environment variable SOURCE_BUCKET is not set.")
    if not destination_bucket:
        raise EnvironmentError("Environment variable DESTINATION_BUCKET is not set.")

    base_prefix = os.environ.get("BASE_PREFIX", DEFAULT_BASE_PREFIX).rstrip("/") + "/"
    s3_client: "S3Client" = boto3.client("s3")

    try:
        latest_date_prefix = get_latest_date_folder(s3_client, source_bucket, base_prefix)
        date_folder = latest_date_prefix.rstrip("/").split("/")[-1]

        ip_prefixes = list_ip_folders(s3_client, source_bucket, latest_date_prefix)
        if not ip_prefixes:
            msg = f"No IP address folders found under {latest_date_prefix}."
            logger.warning(msg)
            send_sns_notification(subject="SCAP Collector — No IP Folders Found", message=msg, success=False)
            return {"statusCode": 200, "body": msg}

        all_xml_keys = [key for ip in ip_prefixes for key in find_xml_files(s3_client, source_bucket, ip)]
        logger.info("Total XML files collected: %d", len(all_xml_keys))

        if not all_xml_keys:
            msg = f"No XML files found in any IP folder under {latest_date_prefix}. Nothing to zip."
            logger.warning(msg)
            send_sns_notification(subject="SCAP Collector — No XML Files Found", message=msg, success=False)
            return {"statusCode": 200, "body": msg}

        zip_buffer, files_zipped = download_and_zip_files(s3_client, source_bucket, all_xml_keys)
        logger.info("Zipped %d file(s) successfully.", files_zipped)

        dest_prefix = os.environ.get("DESTINATION_PREFIX", "XML_/").rstrip("/") + "/"
        timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination_key = f"{dest_prefix}scap_results_{date_folder}_{timestamp}.zip"
        upload_zip(s3_client, destination_bucket, zip_buffer, destination_key)

        success_msg = f"Uploaded {files_zipped} XML file(s) to s3://{destination_bucket}/{destination_key}"
        logger.info(success_msg)
        send_sns_notification(subject=f"SCAP Collector — Success ({date_folder})", message=success_msg, success=True)
        return {"statusCode": 200, "body": success_msg}

    except Exception:
        logger.exception("Unhandled exception in lambda_handler.")
        send_sns_notification(
            subject="SCAP Collector — FAILED",
            message="An unhandled exception occurred. Check CloudWatch logs for details.",
            success=False,
        )
        raise


def _detect_trigger(event: dict) -> str:
    if event.get("Records", [{}])[0].get("eventSource") == "aws:s3":
        return "S3 Event"
    if event.get("source") == "aws.events":
        return "EventBridge"
    return "Unknown / Direct Invocation"


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")

    aws_profile = os.environ.get("AWS_PROFILE")
    if aws_profile:
        boto3.setup_default_session(profile_name=aws_profile)
        logger.info("Using AWS profile: %s", aws_profile)

    mock_event: dict = {"source": "aws.events", "detail-type": "Scheduled Event", "detail": {}}

    class MockContext:
        function_name = "s3-scap-xml-collector"
        aws_request_id = "local-test-request-id"
        log_stream_name = "local-test"

    print("=" * 60)
    print("Running lambda_handler locally …")
    print("=" * 60)
    print(json.dumps(lambda_handler(mock_event, MockContext()), indent=2))
