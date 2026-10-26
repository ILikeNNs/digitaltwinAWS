<#
.SYNOPSIS
  Deploys the serverless version (infra/serverless.yaml): static UI + streaming Lambda + DynamoDB + WAF.

.DESCRIPTION
  Thin wrapper around infra/deploy.py, which holds the build/publish/deploy logic so it runs the same here and on
  CodeBuild. Idempotent: safe to re-run after code or UI changes.
    1. serverless/package.py embeds the resume (only when linkedin.pdf changed) and builds an arm64 Lambda zip
    2. the zip goes to the shared private artifact bucket under lambda/ (content-hashed key -> only real changes redeploy)
    3. CloudFormation deploys the stack (plus the WAF stack in us-east-1 unless -NoWaf)
    4. serverless/web is synced to the site bucket and CloudFront is invalidated

  First run only: creates the artifact bucket and the OPENAI_API_KEY secret (shared with the EC2 stack).
  The OpenAI key is also needed locally (in .env) to embed the resume at build time.

  -NotificationEmail is remembered by the stack: omit it on later deploys to keep the current subscription.

.EXAMPLE
  .\infra\deploy-serverless.ps1 -NotificationEmail me@example.com
  .\infra\deploy-serverless.ps1 -Region eu-central-1 -NoWaf
#>
param(
    [string]$StackName = "digitaltwin-serverless",
    [string]$Region = "",
    [string]$SecretName = "digitaltwin/openai-api-key",
    [string]$NotificationEmail = $null,
    [switch]$NoWaf
)

$arguments = @("deploy", "serverless", "--stack-name", $StackName, "--secret-name", $SecretName)
if ($Region) { $arguments += @("--region", $Region) }
if ($PSBoundParameters.ContainsKey("NotificationEmail")) { $arguments += @("--notification-email", $NotificationEmail) }
if ($NoWaf) { $arguments += "--no-waf" }

uv run --project (Split-Path $PSScriptRoot -Parent) --group deploy python (Join-Path $PSScriptRoot "deploy.py") @arguments
exit $LASTEXITCODE
