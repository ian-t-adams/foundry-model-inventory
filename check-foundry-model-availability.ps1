#requires -Version 5.1
<#
.SYNOPSIS
Lists Foundry model versions, deployment SKUs and subscription quota by region.
.DESCRIPTION
Requires Azure CLI, az login, and subscription-level permission to read models
and usages. Covers the Microsoft.CognitiveServices model catalog, not every
model in the Foundry marketplace. An empty -Models @() lists the whole catalog.
Use -PassThru to return report objects instead of formatting or saving them,
for example when combining reports from multiple subscriptions.

Remaining is unallocated quota, NOT guaranteed live deployment capacity.
Quota can be shared across versions/models: do not add rows with the same
Region and QuotaName. Units and QuotaDescription preserve the API's meaning.

-IncludePrices uses exact catalog billing-meter IDs, when supplied. Prices are
public consumption rates, not your negotiated rates, and retain their units
and tier thresholds. Missing prices are not zero.
.EXAMPLE
.\check-foundry-model-availability.ps1 -SubscriptionId "<subscription-guid>" -Models "gpt-5.6-sol" -Locations eastus2,swedencentral -IncludePrices
.LINK
https://learn.microsoft.com/rest/api/aiservices/accountmanagement/models/list
.LINK
https://learn.microsoft.com/azure/foundry/openai/how-to/quota
.LINK
https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [guid]$SubscriptionId,
    [string[]]$Models = @("gpt-5.6-sol"),
    [ValidateNotNullOrEmpty()]
    [string[]]$Locations = @(
        "eastus", "eastus2", "centralus", "southcentralus", "northcentralus",
        "westus", "westus3", "switzerlandnorth", "switzerlandwest",
        "francecentral", "germanywestcentral", "swedencentral", "westeurope",
        "uksouth", "australiaeast", "japaneast", "koreacentral",
        "southeastasia", "southindia"
    ),
    [switch]$IncludePrices,
    [switch]$PassThru
)

$ErrorActionPreference = "Stop"
$null = Get-Command az -ErrorAction Stop
$baseUrl = "https://management.azure.com/subscriptions/$SubscriptionId/providers/Microsoft.CognitiveServices"
$apiVersion = "2024-10-01"
$priceCache = @{}

function Get-ArmItems([string]$Url) {
    do {
        $json = az rest --method get --url $Url --subscription $SubscriptionId --output json --only-show-errors 2>&1
        if ($LASTEXITCODE -ne 0) { throw ($json -join "`n") }
        $page = ($json -join "`n") | ConvertFrom-Json
        if ($null -eq $page -or $page.PSObject.Properties.Name -notcontains "value") {
            throw "Unexpected ARM response: missing value array."
        }
        $page.value
        $Url = $page.nextLink
    } while ($Url)
}

function Get-RetailPrices([object[]]$Meters) {
    $ids = @($Meters.meterId | Where-Object { $_ } | Sort-Object -Unique)
    if (-not $ids.Count) { return "No billing meter IDs supplied by catalog" }

    $prices = foreach ($id in $ids) {
        if (-not $priceCache.ContainsKey($id)) {
            try {
                $filter = [uri]::EscapeDataString("meterId eq '$($id.Replace("'", "''"))' and priceType eq 'Consumption'")
                $url = 'https://prices.azure.com/api/retail/prices?api-version=2023-01-01-preview&currencyCode=USD&$filter=' + $filter
                $items = @(
                    do {
                        $page = Invoke-RestMethod -Uri $url -TimeoutSec 60
                        if ($null -eq $page -or $page.PSObject.Properties.Name -notcontains "Items") {
                            throw "Unexpected pricing response: missing Items array."
                        }
                        $page.Items | Where-Object { $_.isPrimaryMeterRegion }
                        $url = $page.NextPageLink
                    } while ($url)
                )
                $priceCache[$id] = if ($items.Count) {
                    ($items | ForEach-Object {
                        "{0}: {1} {2} / {3} (tier >= {4})" -f $_.meterName,
                            $_.retailPrice, $_.currencyCode, $_.unitOfMeasure, $_.tierMinimumUnits
                    }) -join "; "
                } else { "No public consumption price returned for meter $id" }
            } catch {
                Write-Warning "Pricing for meter ${id}: $($_.Exception.Message)"
                $priceCache[$id] = "ERROR for meter ${id}: $($_.Exception.Message)"
            }
        }
        $priceCache[$id]
    }
    $prices -join "; "
}

$results = @(
    foreach ($location in ($Locations | Select-Object -Unique)) {
        Write-Host "Checking $location..." -ForegroundColor Cyan
        try {
            $catalog = @(Get-ArmItems "$baseUrl/locations/$location/models?api-version=$apiVersion")
        } catch {
            Write-Warning "Catalog for ${location}: $($_.Exception.Message)"
            [pscustomobject]@{
                Region = $location; Model = $Models -join ", "; Catalog = "ERROR"
                Notes = $_.Exception.Message
            }
            continue
        }

        foreach ($missing in ($Models | Where-Object { $_ -notin $catalog.model.name })) {
            [pscustomobject]@{
                Region = $location; Model = $missing; Catalog = "Not listed"
                Notes = "Model not returned by the catalog for this subscription/region."
            }
        }
        if (-not $Models.Count -and -not $catalog.Count) {
            [pscustomobject]@{ Region = $location; Catalog = "Empty"; Notes = "No models returned." }
        }
        $selected = @($catalog | Where-Object { -not $Models.Count -or $_.model.name -in $Models })
        if (-not $selected.Count) { continue }

        $quotaByName = @{}
        $quotaError = ""
        try {
            $usages = @(Get-ArmItems "$baseUrl/locations/$location/usages?api-version=$apiVersion")
            foreach ($usage in $usages) { $quotaByName[$usage.name.value] = $usage }
        } catch {
            $quotaByName = @{}
            $quotaError = $_.Exception.Message
            Write-Warning "Quota for ${location}: $quotaError"
        }

        foreach ($entry in $selected) {
            $model = $entry.model
            $skus = @($model.skus | Where-Object { $_ })
            if (-not $skus.Count) { $skus = @($null) }
            foreach ($sku in $skus) {
                $quota = $null
                if ($sku.usageName) { $quota = $quotaByName[$sku.usageName] }
                $known = $null -ne $quota.limit -and $null -ne $quota.currentValue
                $type = switch -Regex ($sku.name) {
                    "^Global" { "Global"; break }
                    "^DataZone" { "DataZone"; break }
                    "^(Standard|ProvisionedManaged|Provisioned|Batch)$" { "Regional"; break }
                    default { "Unknown" }
                }
                $unit = switch -Regex ($quota.name.localizedValue) {
                    "Tokens Per Minute \(thousands\)|One Thousand Tokens Per Minute" { "1K TPM"; break }
                    "Provisioned.*Throughput Unit" { "PTU"; break }
                    default { $quota.unit }
                }
                [pscustomobject]@{
                    Model = $model.name
                    Version = $model.version
                    Region = $location
                    Type = $type
                    SKU = $sku.name
                    Catalog = if ($sku) { "Listed" } else { "No SKUs" }
                    Lifecycle = $model.lifecycleStatus
                    Limit = $quota.limit
                    Allocated = $quota.currentValue
                    Remaining = if ($known) { [math]::Max(0, $quota.limit - $quota.currentValue) } else { $null }
                    Unit = $unit
                    QuotaStatus = if ($quotaError) { "ERROR" } elseif ($known) { "Reported" } else { "Unknown" }
                    QuotaName = $sku.usageName
                    QuotaDescription = $quota.name.localizedValue
                    Kind = $entry.kind
                    Format = $model.format
                    InferenceDeprecation = $model.deprecation.inference
                    SkuDeprecation = $sku.deprecationDate
                    Notes = $quotaError
                    RetailPrices = if ($IncludePrices) { Get-RetailPrices $sku.cost } else { $null }
                }
            }
        }
    }
)

$columns = @(
    "Model", "Version", "Region", "Type", "SKU", "Catalog", "Lifecycle",
    "Limit", "Allocated", "Remaining", "Unit", "QuotaStatus", "QuotaName",
    "QuotaDescription", "Kind", "Format", "InferenceDeprecation", "SkuDeprecation", "Notes"
)
if ($IncludePrices) { $columns += "RetailPrices" }
$results = @($results | Select-Object $columns | Sort-Object Model, Version, Region, SKU)
if ($PassThru) {
    $results
    return
}
$outputPath = Join-Path $PWD ("foundry-model-availability-{0}.csv" -f (Get-Date -Format "yyyyMMdd-HHmmss-fff"))
$results | Export-Csv -NoTypeInformation -Encoding UTF8 -NoClobber -Path $outputPath

$group = @{ Name = "Model / version"; Expression = { "$($_.Model) / $($_.Version)" } }
$state = @{ Name = "State"; Expression = {
    if ($_.Catalog -eq "Listed" -and $_.Lifecycle) {
        $_.Lifecycle -replace "^GenerallyAvailable$", "GA"
    } else { $_.Catalog }
} }
$results | Format-Table -GroupBy $group -Property Region, Type, SKU, $state, Limit, Allocated, Remaining, Unit, QuotaStatus -AutoSize -Wrap | Out-Host
if ($IncludePrices) {
    $results | Where-Object { $_.Catalog -eq "Listed" } |
        Format-Table -GroupBy $group -Property Region, SKU, RetailPrices -AutoSize -Wrap | Out-Host
}
Write-Host "Remaining = unallocated quota, not guaranteed deployment capacity. Blank quota = unknown."
Write-Host "Quota may be shared across versions/models; do not sum rows with the same Region + QuotaName."
Write-Host "Lifecycle, retirement dates, quota descriptions and any errors are included in the CSV."
Write-Host "Saved report (including any errors) to $outputPath" -ForegroundColor Green
