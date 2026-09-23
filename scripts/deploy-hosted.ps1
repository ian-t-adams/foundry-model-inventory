#requires -Version 5.1
<#
.SYNOPSIS
Provisions or updates the hosted, read-only Foundry inventory in one Azure subscription.
.DESCRIPTION
Idempotent. Uses only the Azure CLI profile named by -AzureConfigDir (never the
default profile) and never signs in, signs out or changes the default subscription.

1. Reads the collection scope from the private local config (or parameters) and keeps
   only subscriptions in the hosting tenant: a managed identity reads its own tenant.
2. Registers Microsoft.Web on the hosting subscription when needed.
3. Validates the Bicep template and prints a what-if summary. -ValidateOnly stops here.
4. Creates or updates the single-tenant "Foundry inventory" app registration used by
   App Service authentication (ID token sign-in, no client secret) and its service principal.
5. Deploys infra/main.bicep, keeping the image that GitHub Actions last deployed.
6. Sets the app registration's redirect URI from the web app's actual host name.
7. With -SetGitHubSecrets, stores the deployment settings as GitHub Actions secrets.

It prints resource names and URLs, never tokens or credentials.
.EXAMPLE
.\scripts\deploy-hosted.ps1 -AzureConfigDir .\data\azure-cli -SubscriptionId "<subscription-guid>" -ValidateOnly
.EXAMPLE
.\scripts\deploy-hosted.ps1 -AzureConfigDir .\data\azure-cli -SubscriptionId "<subscription-guid>" -SetGitHubSecrets
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$AzureConfigDir,
    [Parameter(Mandatory)][guid]$SubscriptionId,
    [string]$ConfigPath,
    [guid[]]$CollectSubscriptionId,
    [ValidatePattern('^([01][0-9]|2[0-3]):[0-5][0-9]$')][string]$MorningTime,
    [string]$Location = 'centralus',
    [ValidatePattern('^[-\w._()]{1,90}$')][string]$ResourceGroupName = 'rg-foundry-inventory',
    [string]$TimeZone = 'America/Chicago',
    [ValidatePattern('^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$')][string]$GitHubRepository,
    [string]$GitHubBranch = 'main',
    [string]$AppDisplayName = 'Foundry inventory',
    [switch]$ValidateOnly,
    [switch]$SetGitHubSecrets
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$template = Join-Path $repoRoot 'infra\main.bicep'
$graphApp = '00000003-0000-0000-c000-000000000000'
$signInScopes = @(
    '37f7f235-527c-4136-accd-4a02d197296e', # openid
    '14dad69e-099b-42c9-810b-d002981feec1', # profile
    '64a6cdd6-aab1-4aaf-94b8-3cc8405e90d0'  # email
)
$deploymentName = 'foundry-inventory-hosted'
$scratch = @()

function Invoke-Az {
    param([Parameter(Mandatory)][string[]]$Arguments, [switch]$AllowFailure)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & az @Arguments --only-show-errors 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    $errors = @($output | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] } | ForEach-Object { "$_" })
    $text = (@($output | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] }) -join "`n").Trim()
    if ($code -ne 0) {
        if ($AllowFailure) { return $null }
        $message = ($errors -join ' ').Trim()
        $message = $message -replace '(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer ******'
        throw "az $($Arguments[0..1] -join ' ') failed (exit $code): $message"
    }
    return $text
}

function Invoke-AzJson {
    param([Parameter(Mandatory)][string[]]$Arguments, [switch]$AllowFailure)
    $text = Invoke-Az -Arguments ($Arguments + @('--output', 'json')) -AllowFailure:$AllowFailure
    if ($null -eq $text -or $text -eq '') { return $null }
    return $text | ConvertFrom-Json
}

function New-ScratchFile([string]$Name, $Value) {
    $directory = Join-Path $repoRoot 'data'
    $null = New-Item -ItemType Directory -Force -Path $directory
    $path = Join-Path $directory ("{0}.{1}.json" -f $Name, [guid]::NewGuid().ToString('N'))
    [IO.File]::WriteAllText($path, ($Value | ConvertTo-Json -Depth 12), (New-Object Text.UTF8Encoding($false)))
    $script:scratch += $path
    return $path
}

function Write-Step([string]$Text) { Write-Host "==> $Text" -ForegroundColor Cyan }

$previousConfigDir = $env:AZURE_CONFIG_DIR
try {
    # Credentials: one explicit, existing CLI profile; the default profile is refused.
    if (-not (Test-Path -LiteralPath $AzureConfigDir -PathType Container)) {
        throw "AzureConfigDir does not exist: $AzureConfigDir. Sign in within that profile first; this script never signs in."
    }
    $profileDir = (Resolve-Path -LiteralPath $AzureConfigDir).Path.TrimEnd('\', '/')
    $defaultProfile = [IO.Path]::GetFullPath((Join-Path ([Environment]::GetFolderPath('UserProfile')) '.azure')).TrimEnd('\', '/')
    if ($profileDir -ieq $defaultProfile) {
        throw 'Use a dedicated Azure CLI profile directory, not the default ~/.azure profile.'
    }
    $env:AZURE_CONFIG_DIR = $profileDir
    $null = Get-Command az -CommandType Application -ErrorAction Stop
    if (-not (Test-Path -LiteralPath $template -PathType Leaf)) { throw "Template not found: $template" }

    Write-Step 'Checking the Azure CLI profile and hosting subscription'
    $hostAccount = Invoke-AzJson -Arguments @('account', 'show', '--subscription', "$SubscriptionId") -AllowFailure
    if ($null -eq $hostAccount) {
        throw "The profile cannot read subscription $SubscriptionId. Sign in within $profileDir first; this script never signs in."
    }
    if ($hostAccount.state -ne 'Enabled') { throw "Subscription $($hostAccount.name) is not enabled." }
    $tenantId = ([guid]$hostAccount.tenantId).ToString()
    Write-Host "    Hosting subscription: $($hostAccount.name) (tenant $tenantId)"

    # Scope: explicit subscriptions or the private local config, limited to this tenant.
    $requested = @()
    $configTime = $null
    if ($CollectSubscriptionId) {
        $requested = @($CollectSubscriptionId | ForEach-Object { $_.ToString() })
    } else {
        if (-not $ConfigPath) { $ConfigPath = Join-Path $repoRoot 'data\config.json' }
        if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
            throw "No -CollectSubscriptionId was given and the local config was not found: $ConfigPath"
        }
        $config = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $ConfigPath).Path) | ConvertFrom-Json
        $primary = [string]$config.tenant_id
        $configTime = [string]$config.morning_time
        $skipped = 0
        foreach ($item in @($config.subscriptions)) {
            $itemTenant = if ($item.tenant_id) { [string]$item.tenant_id } else { $primary }
            if ($itemTenant -ieq $tenantId) { $requested += ([guid]$item.id).ToString() } else { $skipped++ }
        }
        if ($skipped) {
            Write-Warning "Skipping $skipped configured subscription(s) in other tenants: the web app's managed identity can read only its own tenant."
        }
    }
    if (-not $requested.Count) { throw "No subscriptions to collect in tenant $tenantId." }
    $subscriptions = @()
    foreach ($id in ($requested | Select-Object -Unique)) {
        $account = Invoke-AzJson -Arguments @('account', 'show', '--subscription', $id) -AllowFailure
        if ($null -eq $account -or $account.tenantId -ine $tenantId -or $account.state -ne 'Enabled') {
            throw "Subscription $id is not an enabled subscription in tenant $tenantId for this profile."
        }
        $subscriptions += [pscustomobject][ordered]@{ id = ([guid]$account.id).ToString(); name = [string]$account.name }
    }
    if (-not $MorningTime) { $MorningTime = if ($configTime -match '^([01][0-9]|2[0-3]):[0-5][0-9]$') { $configTime } else { '07:00' } }
    $scopeJson = [ordered]@{ tenant_id = $tenantId; subscriptions = @($subscriptions); morning_time = $MorningTime } |
        ConvertTo-Json -Depth 5 -Compress
    Write-Host "    Collection scope: $($subscriptions.Count) subscription(s), daily at $MorningTime ($TimeZone)"

    if (-not $GitHubRepository) {
        $origin = (& git -C $repoRoot remote get-url origin 2>$null)
        if ("$origin" -match 'github\.com[:/]([A-Za-z0-9-]+/[A-Za-z0-9._-]+?)(\.git)?/?$') { $GitHubRepository = $Matches[1] }
        else { throw 'Pass -GitHubRepository owner/name; the origin remote is not a GitHub repository.' }
    }
    Write-Host "    Deploying branch: $GitHubRepository@$GitHubBranch"

    Write-Step 'Checking the Microsoft.Web resource provider'
    $state = Invoke-Az -Arguments @('provider', 'show', '--namespace', 'Microsoft.Web', '--subscription', "$SubscriptionId", '--query', 'registrationState', '--output', 'tsv')
    if ($state -ne 'Registered') {
        Write-Host "    Registering Microsoft.Web (was $state)..."
        $null = Invoke-Az -Arguments @('provider', 'register', '--namespace', 'Microsoft.Web', '--subscription', "$SubscriptionId", '--wait')
        Write-Host '    Microsoft.Web registered.'
    } else {
        Write-Host '    Microsoft.Web is registered.'
    }

    Write-Step 'Looking for an existing deployment'
    $currentImage = ''
    $appTag = "foundry-inventory-hosted:$SubscriptionId/$ResourceGroupName".ToLowerInvariant()
    $groupExists = (Invoke-Az -Arguments @('group', 'exists', '--name', $ResourceGroupName, '--subscription', "$SubscriptionId")) -eq 'true'
    if ($groupExists) {
        $sites = @(Invoke-AzJson -Arguments @('resource', 'list', '--resource-group', $ResourceGroupName, '--subscription', "$SubscriptionId",
            '--resource-type', 'Microsoft.Web/sites', '--query', "[?tags.component=='hosted-dashboard'].name"))
        if ($sites.Count -gt 1) { throw "More than one hosted dashboard web app exists in $ResourceGroupName." }
        if ($sites.Count -eq 1) {
            $linuxFx = Invoke-Az -Arguments @('webapp', 'config', 'show', '--resource-group', $ResourceGroupName, '--name', $sites[0],
                '--subscription', "$SubscriptionId", '--query', 'linuxFxVersion', '--output', 'tsv')
            if ($linuxFx -like 'DOCKER|*') { $currentImage = $linuxFx.Substring(7) }
            Write-Host "    Web app $($sites[0]) exists; keeping image: $(if ($currentImage) { $currentImage } else { '(none yet)' })"
        }
    } else {
        Write-Host "    Resource group $ResourceGroupName does not exist yet."
    }
    $filter = "tags/any(t:t eq '$appTag')"
    $apps = @(Invoke-AzJson -Arguments @('ad', 'app', 'list', '--filter', $filter, '--query', '[].{id:id, appId:appId, displayName:displayName}'))
    if ($apps.Count -gt 1) { throw "More than one app registration carries the tag $appTag." }
    $app = if ($apps.Count) { $apps[0] } else { $null }
    Write-Host "    App registration: $(if ($app) { "$($app.displayName) ($($app.appId))" } else { 'not created yet' })"

    $parameters = [ordered]@{
        '$schema' = 'https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#'
        contentVersion = '1.0.0.0'
        parameters = [ordered]@{
            location = @{ value = $Location }
            resourceGroupName = @{ value = $ResourceGroupName }
            authClientId = @{ value = $(if ($app) { $app.appId } else { '00000000-0000-0000-0000-000000000000' }) }
            inventoryScope = @{ value = $scopeJson }
            readerSubscriptionIds = @{ value = @($subscriptions | ForEach-Object { $_.id }) }
            containerImage = @{ value = $currentImage }
            githubRepository = @{ value = $GitHubRepository }
            githubBranch = @{ value = $GitHubBranch }
            timeZone = @{ value = $TimeZone }
        }
    }
    $parameterFile = New-ScratchFile 'deploy-hosted.parameters' $parameters
    $deployArgs = @('--name', $deploymentName, '--location', $Location, '--subscription', "$SubscriptionId",
        '--template-file', $template, '--parameters', "@$parameterFile")

    Write-Step 'Validating the template'
    $validation = Invoke-Az -Arguments (@('deployment', 'sub', 'validate') + $deployArgs + @('--query', 'properties.provisioningState', '--output', 'tsv'))
    Write-Host "    Validation: $validation"
    Write-Step 'What-if (resource IDs)'
    & az deployment sub what-if @deployArgs --result-format ResourceIdOnly --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw 'What-if failed.' }
    if ($ValidateOnly) {
        Write-Host 'Validation complete; -ValidateOnly made no changes beyond provider registration.' -ForegroundColor Green
        return
    }

    Write-Step 'Creating or updating the sign-in app registration'
    $appBody = [ordered]@{
        displayName = $AppDisplayName
        signInAudience = 'AzureADMyOrg'
        tags = @($appTag)
        notes = 'Sign-in for the hosted Foundry inventory dashboard (App Service authentication, ID tokens, no client secret).'
        requiredResourceAccess = @([ordered]@{
            resourceAppId = $graphApp
            resourceAccess = @($signInScopes | ForEach-Object { [ordered]@{ id = $_; type = 'Scope' } })
        })
    }
    if ($app) {
        # The complete web settings, including the redirect URI, are written after deployment.
        $null = Invoke-Az -Arguments @('rest', '--method', 'PATCH', '--url', "https://graph.microsoft.com/v1.0/applications/$($app.id)",
            '--headers', 'Content-Type=application/json', '--body', "@$(New-ScratchFile 'app-update' $appBody)")
        Write-Host "    Updated $AppDisplayName ($($app.appId))."
    } else {
        $appBody.web = [ordered]@{ implicitGrantSettings = [ordered]@{ enableIdTokenIssuance = $true; enableAccessTokenIssuance = $false } }
        $app = Invoke-AzJson -Arguments @('rest', '--method', 'POST', '--url', 'https://graph.microsoft.com/v1.0/applications',
            '--headers', 'Content-Type=application/json', '--body', "@$(New-ScratchFile 'app-create' $appBody)",
            '--query', '{id:id, appId:appId, displayName:displayName}')
        Write-Host "    Created $AppDisplayName ($($app.appId))."
    }
    $principal = Invoke-AzJson -Arguments @('ad', 'sp', 'show', '--id', $app.appId, '--query', '{id:id}') -AllowFailure
    if ($null -eq $principal) {
        $principal = Invoke-AzJson -Arguments @('ad', 'sp', 'create', '--id', $app.appId, '--query', '{id:id}')
        Write-Host '    Created its service principal (any member or guest of the tenant may sign in).'
    } else {
        Write-Host '    Its service principal exists.'
    }
    $parameters.parameters.authClientId = @{ value = $app.appId }
    $parameterFile = New-ScratchFile 'deploy-hosted.parameters' $parameters
    $deployArgs[-1] = "@$parameterFile"

    Write-Step 'Deploying infra/main.bicep (a few minutes)'
    $outputs = Invoke-AzJson -Arguments (@('deployment', 'sub', 'create') + $deployArgs + @('--query', 'properties.outputs'))
    $siteHost = [string]$outputs.siteHostName.value
    if ($siteHost -notmatch '^[a-z0-9.-]+$') { throw 'The deployment did not return the web app host name.' }

    Write-Step 'Setting the sign-in redirect URI'
    $redirect = [ordered]@{ web = [ordered]@{
        redirectUris = @("https://$siteHost/.auth/login/aad/callback")
        homePageUrl = "https://$siteHost/"
        implicitGrantSettings = [ordered]@{ enableIdTokenIssuance = $true; enableAccessTokenIssuance = $false }
    } }
    $null = Invoke-Az -Arguments @('rest', '--method', 'PATCH', '--url', "https://graph.microsoft.com/v1.0/applications/$($app.id)",
        '--headers', 'Content-Type=application/json', '--body', "@$(New-ScratchFile 'app-redirect' $redirect)")
    Write-Host "    Redirect URI: https://$siteHost/.auth/login/aad/callback"

    $secrets = [ordered]@{
        AZURE_CLIENT_ID = [string]$outputs.deployClientId.value
        AZURE_TENANT_ID = $tenantId
        AZURE_SUBSCRIPTION_ID = "$SubscriptionId"
        AZURE_RESOURCE_GROUP = [string]$outputs.resourceGroupName.value
        AZURE_WEBAPP_NAME = [string]$outputs.siteName.value
        AZURE_ACR_NAME = [string]$outputs.registryName.value
    }
    if ($SetGitHubSecrets) {
        Write-Step "Setting GitHub Actions secrets on $GitHubRepository"
        $null = Get-Command gh -CommandType Application -ErrorAction Stop
        foreach ($name in $secrets.Keys) {
            if (-not $secrets[$name]) { throw "No value for $name." }
            & gh secret set $name --repo $GitHubRepository --body $secrets[$name] | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "gh secret set $name failed." }
            Write-Host "    $name set."
        }
    }

    Write-Host ''
    Write-Host 'Hosted Foundry inventory is provisioned.' -ForegroundColor Green
    [pscustomobject][ordered]@{
        ResourceGroup = $secrets.AZURE_RESOURCE_GROUP
        Registry = [string]$outputs.registryLoginServer.value
        Plan = "$([string]$outputs.planName.value) (B1 Linux)"
        WebApp = "https://$siteHost/"
        DeployIdentity = [string]$outputs.deployIdentityName.value
        SignInApp = "$AppDisplayName ($($app.appId))"
        CurrentImage = $(if ($currentImage) { $currentImage } else { 'none yet: merge to main to build and deploy' })
        GitHubSecrets = $(if ($SetGitHubSecrets) { ($secrets.Keys -join ', ') } else { 'not changed (use -SetGitHubSecrets)' })
    } | Format-List
} finally {
    foreach ($path in $scratch) { Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue }
    $env:AZURE_CONFIG_DIR = $previousConfigDir
}
