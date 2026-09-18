#requires -Version 5.1
param(
    [string]$ReportScript = (Join-Path (Split-Path $PSScriptRoot -Parent) "check-foundry-model-availability.ps1"),
    [string]$OutputDirectory = (Join-Path (Split-Path $PSScriptRoot -Parent) ("test-output\report-{0}" -f [guid]::NewGuid().ToString("N")))
)
$ErrorActionPreference = "Stop"
$OutputDirectory = (New-Item -ItemType Directory -Path $OutputDirectory -Force).FullName
$global:FoundryReportTest = @{ checks = 0; armCalls = @(); priceCalls = @(); files = @() }

function Assert([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "FAILED: $Message" }
    $global:FoundryReportTest.checks++
}

$global:FoundryReportTest.skus = @(
    @{ name = "Standard"; usageName = "shared.standard.pool"; cost = @(@{ meterId = "meter-input" }) },
    @{ name = "GlobalStandard"; usageName = "global.pool"; cost = @(@{ meterId = "meter-zero" }) },
    @{ name = "DataZoneStandard"; usageName = "datazone.pool"; cost = @(@{ meterId = "meter-unpublished" }) },
    @{ name = "ProvisionedManaged"; usageName = "shared.provisioned.class"; cost = @(@{ meterId = "meter-error" }) },
    @{ name = "GlobalBatch"; usageName = "batch.pool"; cost = @() },
    @{ name = "DataZoneProvisionedManaged"; usageName = "incomplete.pool"; cost = @(@{ meterId = "meter-malformed" }) },
    @{ name = "FutureSku"; usageName = "unknown.pool"; cost = @() }
)
$global:FoundryReportTest.modelOne = @{
    kind = "OpenAI"
    model = @{
        name = "fixture-gpt"; version = "2026-01-01"; format = "OpenAI"
        lifecycleStatus = "GenerallyAvailable"; skus = $global:FoundryReportTest.skus
        deprecation = @{ inference = "2028-01-01" }
    }
}
$global:FoundryReportTest.modelTwo = @{
    kind = "OpenAI"
    model = @{
        name = "fixture-gpt"; version = "2026-06-01"; format = "OpenAI"
        lifecycleStatus = "Preview"; skus = @($global:FoundryReportTest.skus[0])
    }
}
$global:FoundryReportTest.usages = @(
    @{ name = @{ value = "shared.standard.pool"; localizedValue = "Tokens Per Minute (thousands) - fixture-gpt" }; limit = 100; currentValue = 0; unit = "Count" },
    @{ name = @{ value = "global.pool"; localizedValue = "One Thousand Tokens Per Minute - fixture-gpt - GlobalStandard" }; limit = 50; currentValue = 50; unit = "Count" },
    @{ name = @{ value = "datazone.pool"; localizedValue = "Tokens Per Minute (thousands) - fixture-gpt" }; limit = 0; currentValue = 0; unit = "Count" },
    @{ name = @{ value = "shared.provisioned.class"; localizedValue = "Provisioned Managed Throughput Units" }; limit = 500; currentValue = 100; unit = "Count" },
    @{ name = @{ value = "incomplete.pool"; localizedValue = "Unspecified quota" }; limit = 20; currentValue = $null; unit = "Count" }
)

function az {
    $url = [string]$args[([array]::IndexOf($args, "--url") + 1)]
    $global:FoundryReportTest.armCalls += $url
    $global:LASTEXITCODE = 0
    Assert ($args[0] -eq "rest") "Only read-only ARM calls"
    Assert ($args -contains "--subscription") "Explicit subscription, no account set"

    if ($url -match "/locations/(blocked|partial)/models" -and $url -notmatch "partial") {
        $global:LASTEXITCODE = 1
        return "ERROR: AuthorizationFailed (fixture)"
    }
    if ($url -match "/locations/malformed/models") { return '{"notValue":[]}' }
    if ($url -match "/locations/empty/models") {
        return '{"value":[]}'
    }
    if ($url -match "/locations/.*/models") {
        $value = @($global:FoundryReportTest.modelOne)
        $nextLink = $null
        if ($url -match "/eastus2/") {
            $nextLink = "https://management.azure.com/catalog-page-two"
        }
        if ($url -match "/partial/") {
            $nextLink = "https://management.azure.com/catalog-page-error"
        }
        return (@{ value = $value; nextLink = $nextLink } | ConvertTo-Json -Depth 12)
    }
    if ($url -eq "https://management.azure.com/catalog-page-two") {
        return (@{ value = @(
            $global:FoundryReportTest.modelTwo,
            @{ kind = "AIServices"; model = @{ name = "fixture-no-sku"; version = "1"; format = "Other"; skus = @() } },
            @{ kind = "AIServices"; model = @{ name = "fixture-extra"; version = "1"; format = "Other"; skus = @($global:FoundryReportTest.skus[0]) } }
        ) } | ConvertTo-Json -Depth 12)
    }
    if ($url -eq "https://management.azure.com/catalog-page-error") {
        $global:LASTEXITCODE = 1
        return "ERROR: catalog pagination failed (fixture)"
    }
    if ($url -match "/locations/swedencentral/usages") {
        $global:LASTEXITCODE = 1
        return "ERROR: quota read forbidden (fixture)"
    }
    if ($url -match "/locations/quotabroken/usages") {
        return (@{
            value = @($global:FoundryReportTest.usages[0])
            nextLink = "https://management.azure.com/usage-page-error"
        } | ConvertTo-Json -Depth 8)
    }
    if ($url -eq "https://management.azure.com/usage-page-error") {
        return "not valid json"
    }
    if ($url -match "/locations/eastus2/usages") {
        return (@{
            value = @($global:FoundryReportTest.usages[0])
            nextLink = "https://management.azure.com/usage-page-two"
        } | ConvertTo-Json -Depth 8)
    }
    if ($url -eq "https://management.azure.com/usage-page-two") {
        return (@{ value = @($global:FoundryReportTest.usages[1..4]) } | ConvertTo-Json -Depth 8)
    }
    throw "Unexpected ARM URL in test: $url"
}

function Invoke-RestMethod {
    param([string]$Uri, [int]$TimeoutSec)
    $global:FoundryReportTest.priceCalls += $Uri
    $decoded = [uri]::UnescapeDataString($Uri)
    if ($decoded -match "meter-error") { throw "Pricing unavailable (fixture)" }
    if ($decoded -match "meter-malformed") { return [pscustomobject]@{ unexpected = @() } }
    if ($decoded -match "meter-unpublished") { return [pscustomobject]@{ Items = @(); NextPageLink = $null } }
    if ($decoded -match "meter-zero") {
        return [pscustomobject]@{ Items = @(
            @{ isPrimaryMeterRegion = $true; meterName = "Output"; retailPrice = 0; currencyCode = "USD"; unitOfMeasure = "1K"; tierMinimumUnits = 0 }
        ); NextPageLink = $null }
    }
    if ($decoded -match "price-page-two") {
        return [pscustomobject]@{ Items = @(
            @{ isPrimaryMeterRegion = $true; meterName = "Input"; retailPrice = 0.01; currencyCode = "USD"; unitOfMeasure = "1K"; tierMinimumUnits = 1000 }
        ); NextPageLink = $null }
    }
    if ($decoded -match "meter-input") {
        return [pscustomobject]@{ Items = @(
            @{ isPrimaryMeterRegion = $true; meterName = "Input"; retailPrice = 0.012; currencyCode = "USD"; unitOfMeasure = "1K"; tierMinimumUnits = 0 },
            @{ isPrimaryMeterRegion = $false; meterName = "NONPRIMARY"; retailPrice = 999; currencyCode = "USD"; unitOfMeasure = "1K"; tierMinimumUnits = 0 }
        ); NextPageLink = "https://prices.azure.com/price-page-two" }
    }
    throw "Unexpected price URL in test: $Uri"
}

function Run-Report([hashtable]$Parameters) {
    $before = @(Get-ChildItem -LiteralPath $OutputDirectory -Filter "foundry-model-availability-*.csv" |
        Select-Object -ExpandProperty FullName)
    & $ReportScript -SubscriptionId "00000000-0000-0000-0000-000000000001" @Parameters *>&1 |
        Out-String -Width 220 | Write-Host
    $file = Get-ChildItem -LiteralPath $OutputDirectory -Filter "foundry-model-availability-*.csv" |
        Where-Object { $_.FullName -notin $before } | Select-Object -First 1
    Assert ($null -ne $file) "CSV report produced"
    $global:FoundryReportTest.files += $file.FullName
    @(Import-Csv -LiteralPath $file.FullName)
}

Push-Location $OutputDirectory
try {
    $rows = @(Run-Report @{
        Models = @("fixture-gpt", "fixture-no-sku", "fixture-absent")
        Locations = @("eastus2", "swedencentral", "blocked", "empty", "partial", "quotabroken", "malformed", "eastus2")
        IncludePrices = $true
    })
    $standard = @($rows | Where-Object { $_.Region -eq "eastus2" -and $_.SKU -eq "Standard" })
    Assert ($standard.Count -eq 2) "Model pagination returns both versions; repeated region is deduplicated"
    Assert ($standard[0].Remaining -eq "100") "Zero allocation is valid; quota joined by catalog usageName"
    Assert ($standard[1].Remaining -eq "100") "Quota is shared between versions, not split or duplicated arithmetically"
    Assert ($standard[0].Unit -eq "1K TPM") "Only explicitly labeled thousands of TPM receive 1K TPM units"
    Assert ($standard[0].Lifecycle -eq "GenerallyAvailable") "Lifecycle preserved"
    Assert ($standard[0].InferenceDeprecation -eq "2028-01-01") "Retirement metadata preserved"
    Assert ($standard[0].RetailPrices -match "0.012 USD / 1K") "Retail price retains original unit"
    Assert ($standard[0].RetailPrices -match "tier >= 1000") "Price pagination and tiers preserved"
    Assert ($standard[0].RetailPrices -notmatch "NONPRIMARY") "Nonprimary prices excluded"

    $regional = $rows | Where-Object { $_.Region -eq "eastus2" -and $_.SKU -eq "ProvisionedManaged" }
    Assert ($regional.Type -eq "Regional" -and $regional.Remaining -eq "400" -and $regional.Unit -eq "PTU") "Provisioned pooled quota and scope"
    Assert ($regional.RetailPrices -match "^ERROR") "Price failures explicit in exported data"
    $global = $rows | Where-Object { $_.Region -eq "eastus2" -and $_.SKU -eq "GlobalStandard" }
    Assert ($global.Type -eq "Global" -and $global.Remaining -eq "0") "Exhausted global quota"
    Assert ($global.Unit -eq "1K TPM") "Live One Thousand Tokens Per Minute label is recognized"
    Assert ($global.RetailPrices -match "Output: 0 USD / 1K") "Zero retail price retained"
    $datazone = $rows | Where-Object { $_.Region -eq "eastus2" -and $_.SKU -eq "DataZoneStandard" }
    Assert ($datazone.Type -eq "DataZone" -and $datazone.Remaining -eq "0" -and $datazone.QuotaStatus -eq "Reported") "True zero quota is known, not missing"
    Assert ($datazone.RetailPrices -match "^No public consumption price") "Missing price never becomes zero"
    $batch = $rows | Where-Object { $_.Region -eq "eastus2" -and $_.SKU -eq "GlobalBatch" }
    Assert ($batch.Type -eq "Global" -and $batch.Remaining -eq "" -and $batch.QuotaStatus -eq "Unknown") "Unreported quota is blank, not zero"
    Assert ($batch.RetailPrices -eq "No billing meter IDs supplied by catalog") "Missing meter IDs explicitly reported"
    $incomplete = $rows | Where-Object { $_.Region -eq "eastus2" -and $_.SKU -eq "DataZoneProvisionedManaged" }
    Assert ($incomplete.QuotaStatus -eq "Unknown" -and $incomplete.Remaining -eq "") "Incomplete usage cannot fabricate headroom"
    Assert ($incomplete.RetailPrices -match "^ERROR") "Malformed pricing response is an error"
    $future = $rows | Where-Object { $_.Region -eq "eastus2" -and $_.SKU -eq "FutureSku" }
    Assert ($future.Type -eq "Unknown") "Unknown SKUs are not assumed regional"
    Assert (@($rows | Where-Object { $_.Region -eq "eastus2" -and $_.Catalog -eq "No SKUs" }).Count -eq 1) "No-SKU model retained"
    Assert (@($rows | Where-Object { $_.Region -eq "eastus2" -and $_.Model -eq "fixture-absent" -and $_.Catalog -eq "Not listed" }).Count -eq 1) "Absent model retained"
    Assert (@($rows | Where-Object { $_.Region -eq "swedencentral" -and $_.Catalog -eq "Listed" -and $_.QuotaStatus -eq "ERROR" }).Count -eq 7) "Quota failure preserves catalog"
    Assert (@($rows | Where-Object { $_.Region -eq "quotabroken" -and $_.Allocated -ne "" }).Count -eq 0) "Failed quota pagination cannot leave partial known quota"
    foreach ($region in @("blocked", "partial", "malformed")) {
        $failed = @($rows | Where-Object { $_.Region -eq $region })
        Assert ($failed.Count -eq 1 -and $failed[0].Catalog -eq "ERROR") "$region catalog failure is not reported as absence or partial success"
    }
    Assert (@($global:FoundryReportTest.priceCalls | Where-Object { [uri]::UnescapeDataString($_) -match "meter-input" }).Count -eq 1) "Exact meter lookups cached across versions and regions"
    Assert (@($global:FoundryReportTest.priceCalls | Where-Object { [uri]::UnescapeDataString($_) -match "meter-error" }).Count -eq 1) "Failed meter lookup cached without retry loops"

    $priceCallCount = $global:FoundryReportTest.priceCalls.Count
    $all = @(Run-Report @{ Models = @(); Locations = @("eastus2") })
    Assert (@($all | Where-Object { $_.Model -eq "fixture-extra" }).Count -eq 1) "Empty model filter discovers all catalog models"
    Assert ($all[0].PSObject.Properties.Name -notcontains "RetailPrices") "No pricing column when not requested"
    Assert ($global:FoundryReportTest.priceCalls.Count -eq $priceCallCount) "Pricing is opt-in"
    $empty = @(Run-Report @{ Models = @(); Locations = @("empty") })
    Assert ($empty.Count -eq 1 -and $empty[0].Catalog -eq "Empty") "Empty catalog still produces a useful report"
    Assert (@($rows[0].PSObject.Properties.Name).Count -eq 20) "All rows share the complete CSV schema"
    $csvCount = @(Get-ChildItem -LiteralPath $OutputDirectory -Filter "foundry-model-availability-*.csv").Count
    $objects = @(& $ReportScript -SubscriptionId "00000000-0000-0000-0000-000000000001" -Models @() -Locations eastus2 -PassThru)
    Assert ($objects.Count -eq $all.Count) "Object output contains the entire report"
    Assert (@($objects | Where-Object { $_.PSObject.Properties.Name -notcontains "Model" }).Count -eq 0) "Object output contains no formatting records or status messages"
    Assert (@(Get-ChildItem -LiteralPath $OutputDirectory -Filter "foundry-model-availability-*.csv").Count -eq $csvCount) "Object output does not create an extra CSV"
    $number = $objects | Where-Object { $_.SKU -eq "Standard" } | Select-Object -First 1
    Assert ($number.Remaining -is [int] -or $number.Remaining -is [long] -or $number.Remaining -is [double] -or $number.Remaining -is [decimal]) "Object output preserves numeric quota values"
    Write-Host "PASS: $($global:FoundryReportTest.checks) checks on PowerShell $($PSVersionTable.PSVersion)."
} finally {
    Pop-Location
}
