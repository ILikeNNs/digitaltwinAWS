# Shared helpers for destroy.ps1 (EC2) and destroy-serverless.ps1. Dot-source this file.
# (Building and deploying lives in deploy.py, so it runs the same locally and on CodeBuild.)
#
# Native commands are checked via $LASTEXITCODE rather than $ErrorActionPreference = "Stop",
# because Windows PowerShell 5.1 turns redirected stderr from aws.exe into terminating errors.

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

# ---------------------------------------------------------------- teardown helpers (destroy*.ps1)

function Test-StackExists([string]$StackName, [string]$Region) {
    $status = aws cloudformation describe-stacks --region $Region --stack-name $StackName --query "Stacks[0].StackStatus" --output text 2>$null
    return ($LASTEXITCODE -eq 0 -and $status -and $status -ne "DELETE_COMPLETE")
}

# Asks the operator to type the stack name, so a destroy is never one accidental keypress away
function Confirm-Destroy([string]$StackName, [string]$Region, [string[]]$Effects, [switch]$Force) {
    Write-Host "This permanently deletes, in account $(Get-AccountId) (${Region}):" -ForegroundColor Yellow
    $Effects | ForEach-Object { Write-Host "  - $_" }
    if ($Force) { return }
    $typed = Read-Host "Type the stack name ($StackName) to continue"
    if ($typed -ne $StackName) { throw "Confirmation did not match; nothing was deleted." }
}

# Physical ID of a resource while its stack exists. Retained resources outlive their stack, so callers also need a
# fallback for re-runs; note CloudFormation's aws:cloudformation:* tags are NOT applied to every type (e.g. DynamoDB tables).
function Get-StackPhysicalId([string]$StackName, [string]$Region, [string]$LogicalId) {
    if (-not (Test-StackExists $StackName $Region)) { return $null }
    $id = aws cloudformation describe-stack-resource --region $Region --stack-name $StackName --logical-resource-id $LogicalId `
        --query "StackResourceDetail.PhysicalResourceId" --output text 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $id -or $id -eq "None") { return $null }
    return $id
}

# Splits CLI text output into a clean array (drops blanks and the literal "None" the CLI prints for empty results)
function ConvertTo-IdList([string]$Text) {
    return @(($Text -split "\s+") | Where-Object { $_ -and $_ -ne "None" })
}

# Deletes a stack and waits for it; returns $true on success, otherwise prints the resources that failed
function Remove-Stack([string]$StackName, [string]$Region) {
    if (-not (Test-StackExists $StackName $Region)) {
        Write-Host "Stack $StackName not found in $Region (already deleted)"
        return $true
    }
    Write-Host "Deleting stack $StackName in $Region (CloudFront distributions alone can take 15+ minutes)..."
    aws cloudformation delete-stack --region $Region --stack-name $StackName
    Assert-LastExit "delete-stack $StackName"
    do {
        # The waiter gives up after an hour; keep waiting while CloudFormation is still working
        aws cloudformation wait stack-delete-complete --region $Region --stack-name $StackName 2>$null
        $status = aws cloudformation describe-stacks --region $Region --stack-name $StackName --query "Stacks[0].StackStatus" --output text 2>$null
    } while ($LASTEXITCODE -eq 0 -and $status -eq "DELETE_IN_PROGRESS")
    if (-not (Test-StackExists $StackName $Region)) {
        Write-Host "Stack $StackName deleted"
        return $true
    }
    Write-Warning "Stack $StackName did not delete cleanly ($status). Failed resources:"
    aws cloudformation describe-stack-events --region $Region --stack-name $StackName `
        --query "StackEvents[?ResourceStatus=='DELETE_FAILED'].[LogicalResourceId,ResourceStatusReason]" --output text | Out-Host
    return $false
}

# Deletes every object version and delete marker, then the bucket itself
function Remove-VersionedBucket([string]$Bucket, [string]$Region) {
    aws s3api head-bucket --bucket $Bucket --region $Region 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Bucket $Bucket not found (already deleted)"
        return
    }
    Write-Host "Emptying and deleting bucket $Bucket"
    foreach ($kind in "Versions", "DeleteMarkers") {
        while ($true) {
            $page = aws s3api list-object-versions --bucket $Bucket --region $Region --max-items 1000 `
                --query "{Objects: $kind[].{Key: Key, VersionId: VersionId}}" --output json | ConvertFrom-Json
            Assert-LastExit "list-object-versions $Bucket"
            if (-not $page.Objects) { break }
            $batch = New-TemporaryFile
            [IO.File]::WriteAllText($batch, (@{ Objects = @($page.Objects); Quiet = $true } | ConvertTo-Json -Depth 4 -Compress))
            aws s3api delete-objects --bucket $Bucket --region $Region --delete "file://$batch" | Out-Null
            $exit = $LASTEXITCODE
            Remove-Item $batch
            if ($exit -ne 0) { throw "delete-objects on $Bucket failed" }
        }
    }
    aws s3api delete-bucket --bucket $Bucket --region $Region
    Assert-LastExit "delete-bucket $Bucket"
}

# The OpenAI secret and the artifact bucket are shared by both app stacks; only remove them once neither exists
function Remove-SharedResources([string]$Region, [string]$SecretName, [string[]]$AppStacks) {
    $remaining = @($AppStacks | Where-Object { Test-StackExists $_ $Region })
    if ($remaining.Count -gt 0) {
        Write-Warning "Keeping the OpenAI secret and artifact bucket: still used by $($remaining -join ', ')"
        return
    }
    Remove-VersionedBucket "digitaltwin-artifacts-$(Get-AccountId)-$Region" $Region
    aws secretsmanager describe-secret --secret-id $SecretName --region $Region 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        # A recovery window instead of a forced delete: 'aws secretsmanager restore-secret' undoes it for 7 days
        aws secretsmanager delete-secret --secret-id $SecretName --region $Region --recovery-window-in-days 7 | Out-Null
        Assert-LastExit "delete-secret $SecretName"
        Write-Host "Scheduled secret $SecretName for deletion in 7 days (restore with: aws secretsmanager restore-secret --secret-id $SecretName)"
    }
}
