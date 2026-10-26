<#
.SYNOPSIS
  Tears down the EC2 deployment (infra/digitaltwin.yaml) and its WAF stack.

.DESCRIPTION
  Order matters: the app stack goes first (CloudFront must release the web ACL and the VPC origin), then the WAF
  stack in us-east-1.

  CloudFront VPC origins place service-managed networking in the VPC. If a leftover CloudFront security group blocks
  the VPC from being deleted, the script removes it and retries once.

  Kept unless asked otherwise:
    - the EBS data volume (conversation logs + contact requests; retained)   -> -DeleteData removes it
    - the OpenAI secret and artifact bucket shared with the serverless version -> -DeleteShared removes them,
      but only when neither app stack exists any more

  Safe to re-run after a partial failure: retained resources are looked up through the stack while it exists,
  and by their CloudFormation-generated names (or Name tag) once it is gone.

.EXAMPLE
  .\infra\destroy.ps1
  .\infra\destroy.ps1 -DeleteData -DeleteShared
#>
param(
    [string]$StackName = "digitaltwin",
    [string]$Region = "",
    [string]$SecretName = "digitaltwin/openai-api-key",
    [string]$OtherStackName = "digitaltwin-serverless",
    [switch]$DeleteData,
    [switch]$DeleteShared,
    [switch]$Force
)

. (Join-Path $PSScriptRoot "common.ps1")

$Region = Resolve-Region $Region
$wafStack = "$StackName-waf"

# Find the retained resources now. While the stack exists CloudFormation names them; on a re-run after the stack is
# gone, fall back to the Name tag the template gives the data volume (<stack>-data).
$volumes = ConvertTo-IdList (Get-StackPhysicalId $StackName $Region "DataVolume")
if ($volumes.Count -eq 0) {
    $volumes = ConvertTo-IdList (aws ec2 describe-volumes --region $Region --filters "Name=tag:Name,Values=$StackName-data" `
        --query "Volumes[].VolumeId" --output text)
}
$vpcId = Get-StackPhysicalId $StackName $Region "Vpc"

$effects = @("stack $StackName (EC2 instance, NAT gateway, VPC, CloudFront distribution + VPC origin)")
$effects += "stack $wafStack in $WafRegion (WAF web ACL)"
if ($DeleteData) { $effects += $volumes | ForEach-Object { "EBS volume $_ INCLUDING ALL CONTACT REQUESTS AND CONVERSATION LOGS" } }
if ($DeleteShared) { $effects += "OpenAI secret $SecretName and the artifact bucket, if $OtherStackName is gone too" }
Confirm-Destroy $StackName $Region $effects -Force:$Force

if (-not (Remove-Stack $StackName $Region)) {
    # Most likely the VPC: CloudFront creates its own security group for VPC origins, which CloudFormation doesn't own
    $leftovers = @()
    if ($vpcId -and $vpcId -ne "None") {
        $leftovers = @((aws ec2 describe-security-groups --region $Region --filters "Name=vpc-id,Values=$vpcId" `
            --query "SecurityGroups[?starts_with(GroupName, 'CloudFront-VPCOrigins')].GroupId" --output text) -split "\s+" | Where-Object { $_ })
    }
    if ($leftovers.Count -eq 0) { throw "Stack deletion failed; fix the resources above and re-run. Retained data is untouched." }
    foreach ($sg in $leftovers) {
        Write-Host "Removing leftover CloudFront VPC origin security group $sg"
        aws ec2 delete-security-group --region $Region --group-id $sg
        Assert-LastExit "delete-security-group $sg"
    }
    if (-not (Remove-Stack $StackName $Region)) { throw "Stack deletion failed again; see above and re-run. Retained data is untouched." }
}
if (-not (Remove-Stack $wafStack $WafRegion)) { throw "WAF stack deletion failed; see above and re-run." }

foreach ($volume in $volumes) {
    if ($DeleteData) {
        Write-Host "Deleting EBS volume $volume"
        aws ec2 wait volume-available --region $Region --volume-ids $volume
        aws ec2 delete-volume --region $Region --volume-id $volume
        Assert-LastExit "delete-volume $volume"
    } else {
        Write-Host "Kept EBS volume $volume (contact requests). Re-run with -DeleteData to remove it."
    }
}

if ($DeleteShared) { Remove-SharedResources $Region $SecretName @($StackName, $OtherStackName) }
Write-Host "Done."
# The last aws call may have been an expected "not found"; don't let its exit code mark a clean teardown as failed
exit 0
