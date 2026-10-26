<#
.SYNOPSIS
  Tears down the serverless deployment (infra/serverless.yaml) and its WAF stack.

.DESCRIPTION
  Order matters: the app stack goes first (CloudFront must release the web ACL), then the site bucket it retains,
  then the WAF stack in us-east-1.

  Kept unless asked otherwise:
    - the DynamoDB table (contact requests; retained with deletion protection)  -> -DeleteData removes it
    - the OpenAI secret and artifact bucket shared with the EC2 version         -> -DeleteShared removes them,
      but only when neither app stack exists any more

  Safe to re-run after a partial failure: retained resources are looked up through the stack while it exists,
  and by their CloudFormation-generated names (or Name tag) once it is gone.

.EXAMPLE
  .\infra\destroy-serverless.ps1
  .\infra\destroy-serverless.ps1 -DeleteData -DeleteShared
#>
param(
    [string]$StackName = "digitaltwin-serverless",
    [string]$Region = "",
    [string]$SecretName = "digitaltwin/openai-api-key",
    [string]$OtherStackName = "digitaltwin",
    [switch]$DeleteData,
    [switch]$DeleteShared,
    [switch]$Force
)

. (Join-Path $PSScriptRoot "common.ps1")

$Region = Resolve-Region $Region
$wafStack = "$StackName-waf"

# Find the retained resources now. While the stack exists CloudFormation names them; on a re-run after the stack is
# gone, fall back to CloudFormation's generated names: <stack>-<LogicalId>-<suffix> (buckets are lowercased).
$siteBuckets = ConvertTo-IdList (Get-StackPhysicalId $StackName $Region "SiteBucket")
if ($siteBuckets.Count -eq 0) {
    $siteBuckets = ConvertTo-IdList (aws s3api list-buckets --query "Buckets[?starts_with(Name, '$($StackName.ToLower())-sitebucket-')].Name" --output text)
}
$tables = ConvertTo-IdList (Get-StackPhysicalId $StackName $Region "Table")
if ($tables.Count -eq 0) {
    $tables = ConvertTo-IdList (aws dynamodb list-tables --region $Region --query "TableNames[?starts_with(@, '$StackName-Table-')]" --output text)
}

$effects = @("stack $StackName (Lambda, Function URL, CloudFront, alarms, Pipe, SNS topic, KMS key scheduled for deletion)")
$effects += $siteBuckets | ForEach-Object { "site bucket $_ (static UI only; redeployable from the repo)" }
$effects += "stack $wafStack in $WafRegion (WAF web ACL)"
if ($DeleteData) { $effects += $tables | ForEach-Object { "DynamoDB table $_ INCLUDING ALL CONTACT REQUESTS AND CONVERSATION LOGS" } }
if ($DeleteShared) { $effects += "OpenAI secret $SecretName and the artifact bucket, if $OtherStackName is gone too" }
Confirm-Destroy $StackName $Region $effects -Force:$Force

if (-not (Remove-Stack $StackName $Region)) { throw "Stack deletion failed; fix the resources above and re-run. Retained data is untouched." }
foreach ($bucket in $siteBuckets) { Remove-VersionedBucket $bucket $Region }
if (-not (Remove-Stack $wafStack $WafRegion)) { throw "WAF stack deletion failed; see above and re-run." }

foreach ($table in $tables) {
    if ($DeleteData) {
        Write-Host "Deleting DynamoDB table $table"
        aws dynamodb update-table --table-name $table --region $Region --no-deletion-protection-enabled | Out-Null
        Assert-LastExit "disable deletion protection on $table"
        aws dynamodb delete-table --table-name $table --region $Region | Out-Null
        Assert-LastExit "delete-table $table"
        aws dynamodb wait table-not-exists --table-name $table --region $Region
    } else {
        Write-Host "Kept DynamoDB table $table (contact requests). Re-run with -DeleteData to remove it."
    }
}

if ($DeleteShared) { Remove-SharedResources $Region $SecretName @($StackName, $OtherStackName) }
Write-Host "Done."
# The last aws call may have been an expected "not found"; don't let its exit code mark a clean teardown as failed
exit 0
