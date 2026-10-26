<#
.SYNOPSIS
  Deploys the EC2 version (infra/digitaltwin.yaml): private EC2 + CloudFront VPC origin + WAF.

.DESCRIPTION
  Thin wrapper around infra/deploy.py, which holds the build/publish/deploy logic so it runs the same here and on
  CodeBuild. Idempotent: unchanged code produces the same package and the stack update is a no-op; a changed
  package is picked up by cfn-hup on the instance within ~1 minute of the stack update.

  First run only: creates the private, versioned artifact bucket and the Secrets Manager secret holding
  OPENAI_API_KEY (read from .env, or prompted for).

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

$arguments = @("deploy", "ec2", "--stack-name", $StackName, "--secret-name", $SecretName)
if ($Region) { $arguments += @("--region", $Region) }
if ($NoWaf) { $arguments += "--no-waf" }

uv run --project (Split-Path $PSScriptRoot -Parent) --group deploy python (Join-Path $PSScriptRoot "deploy.py") @arguments
exit $LASTEXITCODE
