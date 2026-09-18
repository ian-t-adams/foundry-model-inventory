# Foundry model inventory

Read-only PowerShell scripts for Microsoft Foundry model catalogs, deployment
types, subscription quota, and optional public retail pricing.

This repository contains code and synthetic tests only. It does not contain
tenant inventory, live subscription identifiers, reports, credentials, or Azure
authentication state.

## Requirements

- Windows PowerShell 5.1 or PowerShell 7.
- Azure CLI, signed in with `az login --tenant "<tenant-guid>"`.
- Subscription-level permissions to read Cognitive Services models and usages.
  Subscription Reader is sufficient for the inventory workflow; follow your
  organization's least-privilege policy. No write or deployment role is needed.

These scripts use Azure's commercial-cloud management and pricing endpoints.
They do not change the default Azure CLI subscription or deploy resources.

## Selected models and regions

Run from the repository directory:

```powershell
.\check-foundry-model-availability.ps1 `
    -SubscriptionId "<subscription-guid>" `
    -Models "gpt-5.6-sol" `
    -Locations eastus2,swedencentral `
    -IncludePrices
```

Omit `-IncludePrices` to skip pricing. Omit `-Locations` to use the script's
19-region default list, or pass `-Models @()` to return all catalog models.
Exact model names and available versions depend on the subscription and region.

The script prints a grouped console table and writes a timestamped UTF-8 CSV
to the current directory. `-PassThru` returns objects instead, without formatting
or exporting them, for use in another PowerShell pipeline.

## All models, all advertised regions, multiple subscriptions

The helper scans one subscription at a time. It verifies the tenant, discovers
all advertised regional model endpoints, and adds subscription metadata.
It excludes the nonregional `Global` location; this does not exclude Global
deployment SKUs in the regional catalogs.

Supply your own tenant and subscription identifiers at runtime:

```powershell
$ErrorActionPreference = "Stop"
$tenantId = "<tenant-guid>"
$subscriptionIds = @("<subscription-guid-1>", "<subscription-guid-2>")
$reportScript = Join-Path $PWD "check-foundry-model-availability.ps1"
$outputDirectory = Join-Path $PWD ("reports\{0}" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
$null = New-Item -ItemType Directory -Path $outputDirectory

foreach ($subscriptionId in $subscriptionIds) {
    .\run-foundry-subscription-inventory.ps1 `
        -TenantId $tenantId `
        -SubscriptionId $subscriptionId `
        -ReportScript $reportScript `
        -OutputDirectory $outputDirectory
}

$subscriptionIds |
    ForEach-Object {
        Import-Csv -LiteralPath (Join-Path $outputDirectory "$_.csv")
    } |
    Sort-Object Subscription, Model, Version, Region, SKU, Kind |
    Export-Csv -LiteralPath (Join-Path $outputDirectory "all-subscriptions.csv") `
        -NoTypeInformation -Encoding UTF8 -NoClobber
```

Use a new output directory for each run. Existing output files are not
overwritten. Only pass a trusted script to `-ReportScript`.

For each subscription the helper produces:

| File | Contents |
|---|---|
| `<subscription-guid>.csv` | Detailed model, version, region, SKU, lifecycle and quota rows |
| `<subscription-guid>-coverage.csv` | Every scanned region, empty catalogs, errors and unknown quota counts |
| `<subscription-guid>.log` | Local PowerShell transcript for troubleshooting |

The combined CSV is sorted by subscription. Each normal row represents a model
version, region, deployment SKU and account kind. Catalog errors and empty
regions are retained as status rows rather than silently omitted.

## Reading the data

- `Type` is Global, DataZone, Regional, or Unknown; `SKU` preserves the exact
  deployment type. `Kind` distinguishes account kinds that can otherwise look
  like duplicate rows.
- `Limit`, `Allocated`, and `Remaining` are quota values, not token consumption.
  Remaining quota is **not guaranteed deployment capacity**.
- Quota can be shared across models and versions. Do not add rows with the same
  subscription, region and `QuotaName`.
- `Unit` and `QuotaDescription` preserve the API's meaning. `1K TPM` means
  thousands of tokens per minute; PTU means provisioned throughput units.
  Generic `Count` values require consulting `QuotaDescription`.
- Blank quota means unknown, not zero. `QuotaStatus`, `Catalog`, `Notes`, and the
  coverage CSV distinguish missing metadata from request failures.
- Catalogs can include deprecated, deprecating or legacy models. Check lifecycle
  and retirement fields before planning a deployment.
- Pricing is public USD consumption pricing, not negotiated rates. Exact
  billing-meter IDs must be supplied by the catalog. Missing prices are not zero,
  and rates retain their original units and tier thresholds. The full-catalog
  helper does not request pricing.

The scope is the `Microsoft.CognitiveServices` management catalog, not every
model in the Foundry marketplace. Raw ARM JSON responses are not saved.
Timestamps are snapshots; rerun the scripts when current data is needed.

## Security and privacy

The scripts use your existing Azure CLI session for read-only management calls.
They do not request, print, or save access tokens themselves, and have no
embedded credentials. Azure CLI manages its own authentication cache.

Reports and transcripts **do contain operational metadata**, including tenant
and subscription identifiers, quota information, and possibly local file paths.
Store them with appropriate access controls. They are deliberately excluded
from Git, along with common credential files and CLI state directories.
`.gitignore` is an accident-prevention measure, not a security boundary:
do not force-add output, and review `git diff --cached` before publishing changes.

The optional retail-price request sends catalog billing-meter IDs to the public
Azure pricing API. It does not send your Azure access token to that API.

CSV content is data from Azure, not executable content. When importing a report
into spreadsheet software, treat text fields as text and do not enable external
links or active content from untrusted input.

This public repository contains reusable code and synthetic tests only. Keep
tenant-specific reports, logs and credentials private, and review every change
before publishing. A code review is not a guarantee against all vulnerabilities.

## Offline tests

The test harness uses synthetic catalogs, quotas and pricing responses.
It mocks Azure CLI and HTTP requests; no Azure login or live subscription is
required. Run it in a fresh PowerShell process, not by dot-sourcing it:

```powershell
pwsh -NoProfile -File .\tests\test-foundry-report.ps1
powershell.exe -NoProfile -File .\tests\test-foundry-report.ps1
```

The harness writes generated fixtures under the ignored `test-output` directory.
It covers paging, error responses, unknown and zero quota, shared quota pools,
retirement metadata, pricing units/tiers, and object output.

## API references

- [Model catalog](https://learn.microsoft.com/rest/api/aiservices/accountmanagement/models/list)
- [Quota and capacity](https://learn.microsoft.com/azure/foundry/openai/how-to/quota)
- [Azure Retail Prices API](https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices)
