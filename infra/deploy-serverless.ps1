<#
.SYNOPSIS
  Builds the Lambda package, deploys infra/serverless.yaml and publishes the static UI.

.DESCRIPTION
  Idempotent: safe to re-run after code or UI changes.
    1. serverless/package.py embeds the resume (only when linkedin.pdf changed) and builds an arm64 Lambda zip with uv
    2. the zip goes to the shared private artifact bucket under lambda/ (content-hashed key -> only real changes redeploy)
    3. CloudFormation deploys the stack
    4. serverless/web is synced to the site bucket and CloudFront is invalidated

  Also deploys the WAF web ACL stack in us-east-1 (CloudFront requirement) unless -NoWaf is given.

  First run only: creates the artifact bucket and the OPENAI_API_KEY secret (shared with the EC2 stack).
  The OpenAI key is also needed locally (in .env) to embed the resume at build time.

.EXAMPLE
  .\infra\deploy-serverless.ps1 -NotificationEmail me@example.com
  .\infra\deploy-serverless.ps1 -Region eu-central-1 -NoWaf
#>
param(
    [string]$StackName = "digitaltwin-serverless",
    [string]$Region = "",
    [string]$SecretName = "digitaltwin/openai-api-key",
    [string]$NotificationEmail = "",
    [switch]$NoWaf
)

. (Join-Path $PSScriptRoot "common.ps1")

$Region = Resolve-Region $Region
$account = Get-AccountId
$bucket = "digitaltwin-artifacts-$account-$Region"
Write-Host "Account $account, region $Region, bucket $bucket"

Initialize-ArtifactBucket $bucket $Region
Initialize-OpenAISecret $SecretName $Region

# --- 1. Build ------------------------------------------------------------------
$outDir = Join-Path ([IO.Path]::GetTempPath()) "digitaltwin-lambda"
$zip = uv run --project $RepoRoot python (Join-Path $RepoRoot "serverless\package.py") $outDir
Assert-LastExit "serverless/package.py"
$zip = ($zip | Select-Object -Last 1).Trim()
$codeKey = "lambda/" + (Split-Path $zip -Leaf)

# --- 2. Upload -----------------------------------------------------------------
$package = Publish-Artifact $zip $bucket $codeKey $Region

# --- WAF (us-east-1) -------------------------------------------------------------
$webAclArn = ""
if (-not $NoWaf) {
    # One POST per chat message
    $webAclArn = Deploy-WafStack "$StackName-waf" 30
    Write-Host "WAF web ACL $webAclArn"
}

# --- Concurrency cap -------------------------------------------------------------
# Reserved concurrency is the global cap on simultaneous replies. Lambda keeps 100 executions unreserved, so
# accounts with a low quota (new accounts can start at 10) cannot reserve any; deploy without the cap there.
$reservedConcurrency = 5
$accountLimit = [int](aws lambda get-account-settings --region $Region --query AccountLimit.ConcurrentExecutions --output text)
Assert-LastExit "lambda get-account-settings"
if ($accountLimit - $reservedConcurrency -lt 100) {
    Write-Warning "Lambda concurrency quota is $accountLimit, too low to reserve any. Deploying without the global concurrency cap; WAF per-IP limits still apply. Request a quota increase to enable it."
    $reservedConcurrency = 0
}

# --- 3. Deploy -----------------------------------------------------------------
aws cloudformation deploy `
    --region $Region `
    --stack-name $StackName `
    --template-file (Join-Path $PSScriptRoot "serverless.yaml") `
    --capabilities CAPABILITY_IAM `
    --no-fail-on-empty-changeset `
    --parameter-overrides "CodeBucket=$bucket" "CodeKey=$codeKey" "CodeVersionId=$($package.VersionId)" `
        "OpenAIApiKeySecretName=$SecretName" "NotificationEmail=$NotificationEmail" "WebAclArn=$webAclArn" `
        "ReservedConcurrency=$reservedConcurrency"
Assert-LastExit "cloudformation deploy"

# --- 4. Publish the UI ---------------------------------------------------------
$siteBucket = Get-StackOutput $StackName $Region "SiteBucketName"
$distributionId = Get-StackOutput $StackName $Region "DistributionId"
aws s3 sync (Join-Path $RepoRoot "serverless\web") "s3://$siteBucket" --delete --region $Region --only-show-errors
Assert-LastExit "s3 sync of serverless/web"
aws cloudfront create-invalidation --distribution-id $distributionId --paths "/*" | Out-Null
Assert-LastExit "cloudfront create-invalidation"

aws cloudformation describe-stacks --region $Region --stack-name $StackName `
    --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output table
if ($NotificationEmail) { Write-Host "Confirm the SNS subscription email sent to $NotificationEmail to receive contact requests." }
