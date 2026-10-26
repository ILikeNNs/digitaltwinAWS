<#
.SYNOPSIS
  Packages the digital twin, uploads it to S3 and deploys infra/digitaltwin.yaml (private EC2 + CloudFront VPC origin).

.DESCRIPTION
  Idempotent: safe to re-run after code changes. A changed package gets a new S3 key, object version and
  SHA-256, which cfn-hup on the instance picks up within ~1 minute of the stack update.

  First run only: creates the private, versioned artifact bucket and the Secrets Manager secret
  holding OPENAI_API_KEY (read from .env, or prompted for).

  Also deploys the WAF web ACL stack in us-east-1 (CloudFront requirement) unless -NoWaf is given.

.EXAMPLE
  .\infra\deploy.ps1
  .\infra\deploy.ps1 -Region eu-central-1 -NoWaf
#>
param(
    [string]$StackName = "digitaltwin",
    [string]$Region = "",
    [string]$SecretName = "digitaltwin/openai-api-key",
    [switch]$NoWaf
)

. (Join-Path $PSScriptRoot "common.ps1")

$Region = Resolve-Region $Region
$account = Get-AccountId
$bucket = "digitaltwin-artifacts-$account-$Region"
Write-Host "Account $account, region $Region, bucket $bucket"

Initialize-ArtifactBucket $bucket $Region
Initialize-OpenAISecret $SecretName $Region

# --- Package and upload the app -----------------------------------------------
$appFiles = "app.py", "database.py", "helpers.py", "rag_engine.py", "pyproject.toml", "uv.lock", "linkedin.pdf" |
    ForEach-Object { Join-Path $RepoRoot $_ }
$hashes = ($appFiles | ForEach-Object { (Get-FileHash $_ -Algorithm SHA256).Hash }) -join ""
$sha = [Security.Cryptography.SHA256]::Create()
$digest = -join ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($hashes)) | ForEach-Object { $_.ToString("x2") })
$codeKey = "digitaltwin/app-$($digest.Substring(0, 12)).zip"

$zip = Join-Path ([IO.Path]::GetTempPath()) "digitaltwin-$($digest.Substring(0, 12)).zip"
Compress-Archive -Path $appFiles -DestinationPath $zip -Force
# The instance verifies Sha256 before unpacking the package as root
$package = Publish-Artifact $zip $bucket $codeKey $Region
Remove-Item $zip

# --- WAF (us-east-1) -------------------------------------------------------------
$webAclArn = ""
if (-not $NoWaf) {
    # Gradio sends 2 POSTs per chat message
    $webAclArn = Deploy-WafStack "$StackName-waf" 60
    Write-Host "WAF web ACL $webAclArn"
}

# --- Deploy --------------------------------------------------------------------
aws cloudformation deploy `
    --region $Region `
    --stack-name $StackName `
    --template-file (Join-Path $PSScriptRoot "digitaltwin.yaml") `
    --capabilities CAPABILITY_IAM `
    --no-fail-on-empty-changeset `
    --parameter-overrides "CodeBucket=$bucket" "CodeKey=$codeKey" "CodeVersionId=$($package.VersionId)" "CodeSha256=$($package.Sha256)" `
        "OpenAIApiKeySecretName=$SecretName" "WebAclArn=$webAclArn"
Assert-LastExit "cloudformation deploy"

aws cloudformation describe-stacks --region $Region --stack-name $StackName `
    --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output table
