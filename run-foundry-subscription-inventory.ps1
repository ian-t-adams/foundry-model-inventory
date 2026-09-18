#requires -Version 5.1
param(
    [Parameter(Mandatory)][guid]$TenantId,
    [Parameter(Mandatory)][guid]$SubscriptionId,
    [Parameter(Mandatory)][string]$ReportScript,
    [Parameter(Mandatory)][string]$OutputDirectory
)
$ErrorActionPreference = "Stop"

function Get-AzJson([string[]]$Arguments) {
    $json = az @Arguments --subscription $SubscriptionId --output json --only-show-errors 2>&1
    if ($LASTEXITCODE -ne 0) { throw ($json -join "`n") }
    ($json -join "`n") | ConvertFrom-Json
}

$subscription = Get-AzJson @("account", "show")
if ($subscription.tenantId -ne $TenantId -or $subscription.id -ne $SubscriptionId) {
    throw "Subscription metadata does not match the requested tenant and subscription."
}
if ($subscription.state -ne "Enabled") { throw "Subscription is not enabled: $($subscription.name)" }
$prefix = Join-Path $OutputDirectory $SubscriptionId
$started = (Get-Date).ToUniversalTime().ToString("o")
Start-Transcript -LiteralPath "$prefix.log" -NoClobber | Out-Null
try {
    $provider = Get-AzJson @("provider", "show", "--namespace", "Microsoft.CognitiveServices")
    $advertised = @($provider.resourceTypes |
        Where-Object { $_.resourceType -eq "locations/models" } |
        ForEach-Object { $_.locations } |
        Where-Object { $_ -ne "Global" } | Sort-Object -Unique)
    if (-not $advertised.Count) { throw "No regional model endpoints were advertised." }

    $url = "https://management.azure.com/subscriptions/$SubscriptionId/locations?api-version=2022-12-01"
    $knownRegions = @(
        do {
            $page = Get-AzJson @("rest", "--method", "get", "--url", $url)
            if ($null -eq $page.value) { throw "Location metadata is missing its value array." }
            $page.value
            $url = $page.nextLink
        } while ($url)
    )
    $regions = @(
        foreach ($displayName in $advertised) {
            $matches = @($knownRegions | Where-Object {
                $_.displayName -eq $displayName -or $_.name -eq $displayName
            })
            if ($matches.Count -ne 1) { throw "Cannot uniquely resolve region '$displayName'." }
            $matches[0].name
        }
    ) | Sort-Object -Unique

    Write-Host "$($subscription.name): scanning all models in $($regions.Count) regions."
    $rows = @(& $ReportScript -SubscriptionId $SubscriptionId -Models @() -Locations $regions -PassThru |
        Select-Object @{ Name = "Subscription"; Expression = { $subscription.name } },
            @{ Name = "SubscriptionId"; Expression = { $subscription.id } },
            @{ Name = "TenantId"; Expression = { $subscription.tenantId } },
            @{ Name = "ScanStartedUtc"; Expression = { $started } }, *)
    if (-not $rows.Count) { throw "The report returned no rows." }
    $rows | Export-Csv -LiteralPath "$prefix.csv" -NoTypeInformation -Encoding UTF8 -NoClobber

    $coverage = @(
        foreach ($region in $regions) {
            $regionalRows = @($rows | Where-Object { $_.Region -eq $region })
            if (-not $regionalRows.Count) { throw "Report omitted region '$region'." }
            [pscustomobject]@{
                Subscription = $subscription.name
                SubscriptionId = $subscription.id
                Region = $region
                CatalogStatus = if ($regionalRows.Catalog -contains "ERROR") { "ERROR" }
                    elseif ($regionalRows.Catalog -contains "Empty") { "Empty" } else { "Read" }
                Rows = $regionalRows.Count
                QuotaErrors = @($regionalRows | Where-Object { $_.QuotaStatus -eq "ERROR" }).Count
                QuotaUnknown = @($regionalRows | Where-Object { $_.QuotaStatus -eq "Unknown" }).Count
                Notes = ($regionalRows.Notes | Where-Object { $_ } | Sort-Object -Unique) -join "; "
            }
        }
    )
    $coverage | Export-Csv -LiteralPath "$prefix-coverage.csv" -NoTypeInformation -Encoding UTF8 -NoClobber
    [pscustomobject]@{
        Subscription = $subscription.name
        Regions = $regions.Count
        Models = @($rows | Where-Object { $_.Model } | Select-Object Format, Model -Unique).Count
        Rows = $rows.Count
        CatalogErrors = @($coverage | Where-Object { $_.CatalogStatus -eq "ERROR" }).Count
        QuotaErrors = @($rows | Where-Object { $_.QuotaStatus -eq "ERROR" }).Count
        Csv = "$prefix.csv"
    } | ConvertTo-Json
} finally {
    Stop-Transcript | Out-Null
}
