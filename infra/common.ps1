# Shared helpers for deploy.ps1 (EC2) and deploy-serverless.ps1. Dot-source this file.
#
# Native commands are checked via $LASTEXITCODE rather than $ErrorActionPreference = "Stop",
# because Windows PowerShell 5.1 turns redirected stderr from aws.exe into terminating errors.

$RepoRoot = Split-Path $PSScriptRoot -Parent

# App stacks and data live in the EU; only the CloudFront WAF web ACL must be created in us-east-1 (it holds rules, no data)
$DefaultRegion = "eu-north-1"
$WafRegion = "us-east-1"

function Assert-LastExit([string]$what) {
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" }
}

function Resolve-Region([string]$Region) {
    if (-not $Region) { $Region = $DefaultRegion }
    return $Region
}

function Get-AccountId {
    $account = aws sts get-caller-identity --query Account --output text
    Assert-LastExit "aws sts get-caller-identity (are you logged in? try 'aws login')"
    return $account
}

# Private, encrypted, TLS-only bucket for deployment packages
function Initialize-ArtifactBucket([string]$Bucket, [string]$Region) {
    aws s3api head-bucket --bucket $Bucket 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        # Idempotent: stacks pin packages by object version, which requires versioning (also on buckets made by older scripts)
        aws s3api put-bucket-versioning --bucket $Bucket --versioning-configuration Status=Enabled --region $Region
        Assert-LastExit "put-bucket-versioning"
        return
    }

    Write-Host "Creating artifact bucket $Bucket"
    if ($Region -eq "us-east-1") {
        aws s3api create-bucket --bucket $Bucket --region $Region | Out-Null
    } else {
        aws s3api create-bucket --bucket $Bucket --region $Region --create-bucket-configuration LocationConstraint=$Region | Out-Null
    }
    Assert-LastExit "create-bucket"
    aws s3api put-public-access-block --bucket $Bucket --public-access-block-configuration "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
    Assert-LastExit "put-public-access-block"
    aws s3api put-bucket-versioning --bucket $Bucket --versioning-configuration Status=Enabled --region $Region
    Assert-LastExit "put-bucket-versioning"
    $policy = @{
        Version   = "2012-10-17"
        Statement = @(@{
            Sid       = "DenyInsecureTransport"
            Effect    = "Deny"
            Principal = "*"
            Action    = "s3:*"
            Resource  = @("arn:aws:s3:::$Bucket", "arn:aws:s3:::$Bucket/*")
            Condition = @{ Bool = @{ "aws:SecureTransport" = "false" } }
        })
    } | ConvertTo-Json -Depth 6 -Compress
    $policyFile = New-TemporaryFile
    [IO.File]::WriteAllText($policyFile, $policy)
    aws s3api put-bucket-policy --bucket $Bucket --policy "file://$policyFile"
    $exit = $LASTEXITCODE
    Remove-Item $policyFile
    if ($exit -ne 0) { throw "put-bucket-policy failed" }
}

# Secrets Manager secret holding OPENAI_API_KEY (from .env, or prompted for)
function Initialize-OpenAISecret([string]$SecretName, [string]$Region) {
    aws secretsmanager describe-secret --secret-id $SecretName --region $Region 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { return }

    $key = $null
    $envFile = Join-Path $RepoRoot ".env"
    if (Test-Path $envFile) {
        $line = Get-Content $envFile | Where-Object { $_ -match '^\s*OPENAI_API_KEY\s*=\s*(.+)$' } | Select-Object -First 1
        if ($line) { $key = ($line -replace '^\s*OPENAI_API_KEY\s*=\s*', '').Trim().Trim('"', "'") }
    }
    if (-not $key) {
        $secure = Read-Host "OPENAI_API_KEY" -AsSecureString
        $key = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    }
    Write-Host "Creating secret $SecretName"
    $secretFile = New-TemporaryFile
    [IO.File]::WriteAllText($secretFile, $key)
    aws secretsmanager create-secret --name $SecretName --region $Region --secret-string "file://$secretFile" | Out-Null
    $exit = $LASTEXITCODE
    Remove-Item $secretFile
    if ($exit -ne 0) { throw "create-secret failed" }
}

function Get-StackOutput([string]$StackName, [string]$Region, [string]$Key) {
    $value = aws cloudformation describe-stacks --region $Region --stack-name $StackName `
        --query "Stacks[0].Outputs[?OutputKey=='$Key'].OutputValue" --output text
    Assert-LastExit "describe-stacks"
    return $value
}

# Uploads a package unless one with the same (content-hashed) key exists, and returns @{ VersionId; Sha256 }.
# Stacks pin that exact object version, and the EC2 instance verifies the SHA-256 before unpacking.
function Publish-Artifact([string]$File, [string]$Bucket, [string]$Key, [string]$Region) {
    $existing = aws s3api head-object --bucket $Bucket --key $Key --region $Region --query "[VersionId, Metadata.sha256]" --output text 2>$null
    if ($LASTEXITCODE -eq 0) {
        $versionId, $sha256 = $existing -split "\s+"
        if ($versionId -and $versionId -ne "None" -and $sha256 -and $sha256 -ne "None") {
            Write-Host "Package s3://$Bucket/$Key already uploaded (version $versionId)"
            return @{ VersionId = $versionId; Sha256 = $sha256 }
        }
    }
    $sha256 = (Get-FileHash $File -Algorithm SHA256).Hash.ToLowerInvariant()
    $versionId = aws s3api put-object --bucket $Bucket --key $Key --body $File --region $Region `
        --metadata "sha256=$sha256" --query VersionId --output text
    Assert-LastExit "upload of $Key"
    if (-not $versionId -or $versionId -eq "None") { throw "Bucket $Bucket returned no version id; is versioning enabled?" }
    Write-Host "Uploaded s3://$Bucket/$Key (version $versionId)"
    return @{ VersionId = $versionId; Sha256 = $sha256 }
}

# CloudFront-scoped WAF web ACL (infra/waf.yaml); must live in us-east-1 regardless of the app region
function Deploy-WafStack([string]$StackName, [int]$PostRateLimitPer5Min) {
    aws cloudformation deploy `
        --region $WafRegion `
        --stack-name $StackName `
        --template-file (Join-Path $PSScriptRoot "waf.yaml") `
        --no-fail-on-empty-changeset `
        --parameter-overrides "PostRateLimitPer5Min=$PostRateLimitPer5Min" | Out-Host  # keep deploy output out of the return value
    Assert-LastExit "WAF stack deploy"
    return Get-StackOutput $StackName $WafRegion "WebAclArn"
}
