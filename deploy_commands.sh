#!/usr/bin/env bash
# =============================================================================
# deploy_commands.sh
# S3 SCAP XML Collector — AWS CLI deployment script
#
# Usage:
#   chmod +x deploy_commands.sh
#   ./deploy_commands.sh
#
# Prerequisites:
#   - AWS CLI v2 configured with appropriate credentials
#   - Replace all <PLACEHOLDER> values before running
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration — replace these values before running
# ---------------------------------------------------------------------------
FUNCTION_NAME="s3-scap-xml-collector"
RUNTIME="python3.12"
HANDLER="lambda_function.lambda_handler"
TIMEOUT=300          # seconds
MEMORY=512           # MB
REGION="us-east-1"  # change to your target region
ACCOUNT_ID="<YOUR_ACCOUNT_ID>"

SOURCE_BUCKET="<SOURCE_BUCKET_NAME>"
DESTINATION_BUCKET="<DESTINATION_BUCKET_NAME>"
BASE_PREFIX="reports/test/"
MAX_ZIP_SIZE_MB="450"

ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${FUNCTION_NAME}-role"
POLICY_NAME="${FUNCTION_NAME}-policy"

# EventBridge rule name
RULE_NAME="${FUNCTION_NAME}-monthly-schedule"
# Cron: 1st of every month at 06:00 UTC
CRON_EXPRESSION="cron(0 6 1 * ? *)"

echo "========================================================"
echo " Deploying: ${FUNCTION_NAME}"
echo " Region:    ${REGION}"
echo "========================================================"

# ---------------------------------------------------------------------------
# Step 1 — Create IAM execution role (skip if it already exists)
# ---------------------------------------------------------------------------
echo "[1/7] Creating IAM execution role..."

aws iam create-role \
  --role-name "${FUNCTION_NAME}-role" \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "lambda.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }' \
  --region "${REGION}" || echo "  Role may already exist — continuing."

echo "[1/7] Attaching inline policy from iam_policy.json..."
# Substitute placeholders in the policy file before attaching
POLICY_DOC=$(sed \
  -e "s/<SOURCE_BUCKET_NAME>/${SOURCE_BUCKET}/g" \
  -e "s/<DESTINATION_BUCKET_NAME>/${DESTINATION_BUCKET}/g" \
  iam_policy.json)

aws iam put-role-policy \
  --role-name "${FUNCTION_NAME}-role" \
  --policy-name "${POLICY_NAME}" \
  --policy-document "${POLICY_DOC}" \
  --region "${REGION}"

echo "  Waiting for IAM role to propagate..."
sleep 10

# ---------------------------------------------------------------------------
# Step 2 — Package the Lambda code
# ---------------------------------------------------------------------------
echo "[2/7] Packaging Lambda code..."
zip -j function.zip lambda_function.py
echo "  Created function.zip"

# ---------------------------------------------------------------------------
# Step 3 — Create (or update) the Lambda function
# ---------------------------------------------------------------------------
echo "[3/7] Deploying Lambda function..."

if aws lambda get-function --function-name "${FUNCTION_NAME}" --region "${REGION}" \
   > /dev/null 2>&1; then
  echo "  Function exists — updating code..."
  aws lambda update-function-code \
    --function-name "${FUNCTION_NAME}" \
    --zip-file fileb://function.zip \
    --region "${REGION}"
  aws lambda wait function-updated \
    --function-name "${FUNCTION_NAME}" \
    --region "${REGION}"
  echo "  Updating configuration..."
  aws lambda update-function-configuration \
    --function-name "${FUNCTION_NAME}" \
    --timeout "${TIMEOUT}" \
    --memory-size "${MEMORY}" \
    --region "${REGION}"
else
  echo "  Function does not exist — creating..."
  aws lambda create-function \
    --function-name "${FUNCTION_NAME}" \
    --runtime "${RUNTIME}" \
    --role "${ROLE_ARN}" \
    --handler "${HANDLER}" \
    --timeout "${TIMEOUT}" \
    --memory-size "${MEMORY}" \
    --zip-file fileb://function.zip \
    --region "${REGION}"
fi

# ---------------------------------------------------------------------------
# Step 4 — Set environment variables
# ---------------------------------------------------------------------------
echo "[4/7] Setting environment variables..."
aws lambda update-function-configuration \
  --function-name "${FUNCTION_NAME}" \
  --environment "Variables={
    SOURCE_BUCKET=${SOURCE_BUCKET},
    DESTINATION_BUCKET=${DESTINATION_BUCKET},
    BASE_PREFIX=${BASE_PREFIX},
    MAX_ZIP_SIZE_MB=${MAX_ZIP_SIZE_MB}
  }" \
  --region "${REGION}"

aws lambda wait function-updated \
  --function-name "${FUNCTION_NAME}" \
  --region "${REGION}"

# ---------------------------------------------------------------------------
# Step 5 — Add S3 event trigger (source bucket, ObjectCreated, prefix filter)
# ---------------------------------------------------------------------------
echo "[5/7] Adding S3 event trigger..."

LAMBDA_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${FUNCTION_NAME}"

# Allow S3 to invoke the Lambda
aws lambda add-permission \
  --function-name "${FUNCTION_NAME}" \
  --statement-id "AllowS3Invoke" \
  --action "lambda:InvokeFunction" \
  --principal s3.amazonaws.com \
  --source-arn "arn:aws:s3:::${SOURCE_BUCKET}" \
  --source-account "${ACCOUNT_ID}" \
  --region "${REGION}" || echo "  Permission may already exist — continuing."

# Configure the S3 bucket notification
# NOTE: This replaces ALL existing notifications on the bucket.
#       If the bucket already has notifications, merge them manually.
aws s3api put-bucket-notification-configuration \
  --bucket "${SOURCE_BUCKET}" \
  --notification-configuration "{
    \"LambdaFunctionConfigurations\": [
      {
        \"LambdaFunctionArn\": \"${LAMBDA_ARN}\",
        \"Events\": [\"s3:ObjectCreated:*\"],
        \"Filter\": {
          \"Key\": {
            \"FilterRules\": [
              {\"Name\": \"prefix\", \"Value\": \"reports/test/\"}
            ]
          }
        }
      }
    ]
  }"

# ---------------------------------------------------------------------------
# Step 6 — Create EventBridge monthly schedule rule
# ---------------------------------------------------------------------------
echo "[6/7] Creating EventBridge monthly schedule rule..."

aws events put-rule \
  --name "${RULE_NAME}" \
  --schedule-expression "${CRON_EXPRESSION}" \
  --state ENABLED \
  --description "Monthly SCAP XML collection — triggers on 1st of month at 06:00 UTC" \
  --region "${REGION}"

RULE_ARN=$(aws events describe-rule \
  --name "${RULE_NAME}" \
  --region "${REGION}" \
  --query "Arn" \
  --output text)

# Grant EventBridge permission to invoke the Lambda
aws lambda add-permission \
  --function-name "${FUNCTION_NAME}" \
  --statement-id "AllowEventBridgeInvoke" \
  --action "lambda:InvokeFunction" \
  --principal events.amazonaws.com \
  --source-arn "${RULE_ARN}" \
  --region "${REGION}" || echo "  Permission may already exist — continuing."

# Register the Lambda as a target for the rule
aws events put-targets \
  --rule "${RULE_NAME}" \
  --targets "[{\"Id\": \"ScapCollectorLambda\", \"Arn\": \"${LAMBDA_ARN}\"}]" \
  --region "${REGION}"

# ---------------------------------------------------------------------------
# Step 7 — Verify deployment
# ---------------------------------------------------------------------------
echo "[7/7] Verifying deployment..."
aws lambda get-function \
  --function-name "${FUNCTION_NAME}" \
  --region "${REGION}" \
  --query "Configuration.[FunctionName,Runtime,Handler,Timeout,MemorySize,LastModified]" \
  --output table

echo ""
echo "========================================================"
echo " Deployment complete!"
echo " Function ARN: ${LAMBDA_ARN}"
echo " EventBridge rule: ${RULE_NAME} (${CRON_EXPRESSION})"
echo "========================================================"

# ---------------------------------------------------------------------------
# CloudFormation equivalent snippet (for reference)
# ---------------------------------------------------------------------------
# Save this to a separate template and deploy with:
#   aws cloudformation deploy \
#     --template-file eventbridge_rule.yaml \
#     --stack-name scap-collector-schedule \
#     --capabilities CAPABILITY_IAM
#
# --- eventbridge_rule.yaml ---
# AWSTemplateFormatVersion: "2010-09-09"
# Resources:
#   ScapCollectorScheduleRule:
#     Type: AWS::Events::Rule
#     Properties:
#       Name: s3-scap-xml-collector-monthly-schedule
#       ScheduleExpression: "cron(0 6 1 * ? *)"
#       State: ENABLED
#       Targets:
#         - Id: ScapCollectorLambda
#           Arn: !Sub "arn:aws:lambda:${AWS::Region}:${AWS::AccountId}:function:s3-scap-xml-collector"
#   ScapCollectorLambdaPermission:
#     Type: AWS::Lambda::Permission
#     Properties:
#       FunctionName: s3-scap-xml-collector
#       Action: lambda:InvokeFunction
#       Principal: events.amazonaws.com
#       SourceArn: !GetAtt ScapCollectorScheduleRule.Arn
#
# --- Terraform equivalent ---
# resource "aws_cloudwatch_event_rule" "scap_monthly" {
#   name                = "s3-scap-xml-collector-monthly-schedule"
#   schedule_expression = "cron(0 6 1 * ? *)"
#   description         = "Monthly SCAP XML collection trigger"
# }
# resource "aws_cloudwatch_event_target" "scap_lambda" {
#   rule      = aws_cloudwatch_event_rule.scap_monthly.name
#   target_id = "ScapCollectorLambda"
#   arn       = aws_lambda_function.scap_collector.arn
# }
# resource "aws_lambda_permission" "allow_eventbridge" {
#   statement_id  = "AllowEventBridgeInvoke"
#   action        = "lambda:InvokeFunction"
#   function_name = aws_lambda_function.scap_collector.function_name
#   principal     = "events.amazonaws.com"
#   source_arn    = aws_cloudwatch_event_rule.scap_monthly.arn
# }
