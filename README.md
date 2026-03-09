# S3 SCAP XML Collector — Lambda Function

Automates the collection of SCAP XML scan results from a source S3 bucket,
packages them into a single ZIP archive, and uploads the archive to a
destination S3 bucket.

---

## Architecture

```
                        ┌─────────────────────────────────────────────────┐
                        │                  AWS Lambda                      │
  ┌──────────────┐      │                                                 │
  │ EventBridge  │─────▶│  1. Find latest date folder                     │
  │ (monthly     │      │  2. List IP address sub-folders                 │
  │  cron)       │      │  3. Find all .xml files per IP                  │
  └──────────────┘      │  4. Download & zip in memory (io.BytesIO)       │
                        │  5. Upload ZIP to destination bucket            │
  ┌──────────────┐      │                                                 │
  │ S3 Event     │─────▶│                                                 │
  │ (ObjectCreate│      └──────────┬──────────────────────┬──────────────┘
  │  on source   │                 │ s3:GetObject          │ s3:PutObject
  │  bucket)     │                 ▼                       ▼
  └──────────────┘      ┌──────────────────┐   ┌──────────────────────┐
                        │  Source S3 Bucket │   │ Destination S3 Bucket│
                        │  reports/test/    │   │  scap-results/       │
                        │  <DATE>/          │   │  <DATE>/             │
                        │  <IP>/Results/... │   │  scap_results_*.zip  │
                        └──────────────────┘   └──────────────────────┘
```

---

## Files

| File | Description |
|------|-------------|
| `lambda_function.py` | Main Lambda handler and helper functions |
| `iam_policy.json` | Least-privilege IAM policy for the Lambda execution role |
| `requirements.txt` | Python dependencies for local dev and CI |
| `deploy_commands.sh` | End-to-end AWS CLI deployment script |
| `test_lambda.py` | pytest + moto unit and integration tests |

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SOURCE_BUCKET` | **Yes** | — | Name of the S3 bucket containing SCAP XML files |
| `DESTINATION_BUCKET` | **Yes** | — | Name of the S3 bucket for ZIP output |
| `BASE_PREFIX` | No | `reports/test/` | Key prefix that contains date folders |
| `MAX_ZIP_SIZE_MB` | No | `450` | Soft warning threshold; logs a warning if exceeded |
| `SNS_TOPIC_ARN` | No | — | ARN of an SNS topic for success/failure notifications (see below) |

---

## Quick Start

### 1. Clone and install dependencies

```bash
git clone <repo-url>
cd s3-scap-xml-collector
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure your environment

```bash
export SOURCE_BUCKET="my-scap-source-bucket"
export DESTINATION_BUCKET="my-scap-destination-bucket"
# Optional overrides:
export BASE_PREFIX="reports/test/"
export MAX_ZIP_SIZE_MB="450"
```

### 3. Run tests

```bash
pytest test_lambda.py -v
```

### 4. Deploy to AWS

Edit the placeholder values at the top of `deploy_commands.sh`, then:

```bash
chmod +x deploy_commands.sh
./deploy_commands.sh
```

The script will:
1. Create the IAM execution role with the least-privilege policy
2. Package `lambda_function.py` into `function.zip`
3. Create or update the Lambda function with correct runtime / memory / timeout
4. Set all environment variables
5. Add the S3 `ObjectCreated` trigger on the source bucket (prefix `reports/test/`)
6. Create the monthly EventBridge rule (`cron(0 6 1 * ? *)`) and grant invoke permissions
7. Print a verification summary

### 5. Test the deployed function

Invoke it directly from the CLI:

```bash
aws lambda invoke \
  --function-name s3-scap-xml-collector \
  --payload '{"source":"aws.events","detail-type":"Scheduled Event","detail":{}}' \
  --cli-binary-format raw-in-base64-out \
  response.json && cat response.json
```

---

## IAM Policy Setup

Replace the placeholders in `iam_policy.json` before attaching:

```bash
# Substitute bucket names inline
sed \
  -e 's/<SOURCE_BUCKET_NAME>/my-source-bucket/g' \
  -e 's/<DESTINATION_BUCKET_NAME>/my-dest-bucket/g' \
  iam_policy.json > iam_policy_filled.json

aws iam put-role-policy \
  --role-name s3-scap-xml-collector-role \
  --policy-name s3-scap-xml-collector-policy \
  --policy-document file://iam_policy_filled.json
```

---

## EventBridge Monthly Schedule

The EventBridge rule fires on the **1st of every month at 06:00 UTC**:

```
cron(0 6 1 * ? *)
```

AWS CLI command to add the permission grant separately if needed:

```bash
aws lambda add-permission \
  --function-name s3-scap-xml-collector \
  --statement-id AllowEventBridgeInvoke \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn arn:aws:events:us-east-1:<ACCOUNT_ID>:rule/s3-scap-xml-collector-monthly-schedule
```

---

## Source Bucket Path Structure

```
<SOURCE_BUCKET>/
└── reports/
    └── test/
        └── <DATE_FOLDER>/          ← e.g., 2024-01-15
            ├── 192.168.1.100/
            │   └── Results/
            │       └── SCAP/
            │           └── XML/
            │               └── scan_result.xml
            ├── 192.168.1.101/
            │   └── Results/
            │       └── SCAP/
            │           └── XML/
            │               └── scan_result.xml
            └── ...
```

## Destination Bucket Output

```
<DESTINATION_BUCKET>/
└── scap-results/
    └── <DATE_FOLDER>/
        └── scap_results_<DATE_FOLDER>_<TIMESTAMP>.zip
```

Inside the ZIP, files are named `<IP_ADDRESS>/<scan_file>.xml` to preserve
origin information.

---

## Testing

### Unit / integration tests (moto)

```bash
pytest test_lambda.py -v
```

Test cases covered:

| Test | Description |
|------|-------------|
| `test_successful_collection` | Happy path: 3 XMLs across 2 IPs → 1 ZIP |
| `test_zip_contains_correct_files` | ZIP contains all 3 XML entries |
| `test_zip_key_matches_pattern` | Output key matches `scap-results/<DATE>/scap_results_*.zip` |
| `test_no_date_folders_raises` | Raises `ValueError` when no date folders exist |
| `test_no_xml_files_returns_gracefully` | Returns 200 with warning when no XMLs found |

### Local invocation

```bash
AWS_PROFILE=my-profile python lambda_function.py
```

---

## Future SNS Integration

When an SNS topic is ready for SCAP collection notifications, follow these
steps to enable it:

1. **Create the SNS topic** (if it doesn't exist):

   ```bash
   aws sns create-topic --name scap-collector-notifications
   ```

2. **Set the environment variable** on the Lambda:

   ```bash
   aws lambda update-function-configuration \
     --function-name s3-scap-xml-collector \
     --environment "Variables={...,SNS_TOPIC_ARN=arn:aws:sns:us-east-1:<ACCOUNT_ID>:scap-collector-notifications}"
   ```

3. **Add SNS publish permission** to the IAM execution role:

   ```json
   {
     "Sid": "AllowSNSPublish",
     "Effect": "Allow",
     "Action": "sns:Publish",
     "Resource": "arn:aws:sns:us-east-1:<ACCOUNT_ID>:scap-collector-notifications"
   }
   ```

4. **Uncomment the SNS publish call** in `lambda_function.py` inside the
   `send_sns_notification()` function. Look for the comment:

   ```python
   # TODO: Uncomment when SNS topic is ready
   ```

   Uncomment the `sns_client.publish(...)` block. No other code changes are
   required — the function is already called on both the success and failure
   paths in `lambda_handler`.

5. **Subscribe** your email address or downstream system to the topic:

   ```bash
   aws sns subscribe \
     --topic-arn arn:aws:sns:us-east-1:<ACCOUNT_ID>:scap-collector-notifications \
     --protocol email \
     --notification-endpoint your-team@example.com
   ```

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `ValueError: No date folders found` | `BASE_PREFIX` wrong or bucket empty | Verify env var and bucket contents |
| `ClientError: AccessDenied` | IAM policy missing | Attach `iam_policy.json` to execution role |
| ZIP exceeds `MAX_ZIP_SIZE_MB` | Many/large XML files | Increase Lambda memory; consider splitting by IP range |
| Lambda timeout | Too many files to download | Increase `TIMEOUT` or switch to Step Functions |
| No trigger on new scan | S3 notification not set | Re-run `deploy_commands.sh` step 5 |
