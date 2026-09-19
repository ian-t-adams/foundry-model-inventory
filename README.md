# Foundry model inventory

Read-only PowerShell scripts and a lightweight localhost dashboard for Microsoft
Foundry model catalogs, deployment types, subscription quota, and snapshot history.
The standalone script also supports optional public retail pricing.

This repository contains code and synthetic tests only. It does not contain
tenant inventory, live subscription identifiers, reports, credentials, or Azure
authentication state.

## Requirements

- Windows PowerShell 5.1 or PowerShell 7.
- Python 3.11 or newer for the dashboard. It uses only the Python standard
  library: no package installation, frontend build, or database service.
- Azure CLI, signed in with `az login --tenant "<tenant-guid>"`.
- Subscription-level permissions to read Cognitive Services models and usages.
  Subscription Reader is sufficient for the inventory workflow; follow your
  organization's least-privilege policy. No write or deployment role is needed.

These scripts use Azure's commercial-cloud management and pricing endpoints.
They do not change the default Azure CLI subscription or deploy resources.

## Local dashboard

Keep your checkout on a local drive, for example `D:\foundry-model-inventory`.
From that checkout:

```powershell
.\start-dashboard.ps1
```

Open **http://127.0.0.1:8765**. The server binds to loopback only and runs in the
foreground until you press Ctrl+C. Use `-Port` to choose another local port.
There are no external fonts, CDNs, analytics, or browser calls to Azure.

In **Collection & history**, discover the subscriptions from your existing Azure
CLI sign-in, choose one tenant and the subscriptions to collect, and save the
scope. **Collect now** starts a read-only estate scan. You can keep browsing the
previous snapshot while it runs.

The inventory supports subscription, region, model family, exact model/version,
deployment geography, PAYG/PTU/batch, lifecycle, and remaining-quota filters.
Categorical filters are searchable, checkbox-based multi-selects: selections
within a filter are matched with **OR**, and different filters combine with
**AND**. Choose exactly one quota unit before entering a minimum, so a numeric
threshold cannot silently mix TPM and PTUs. Saved views stay in your browser.
Filtered CSV exports contain all matching deployment options, not only the
visible page or grouped summary.

Choose **Model** first, then use **Version** to narrow the selection. The Version
control is disabled until at least one model is selected and only shows versions
of those models. Leaving it at all versions lets you compare them. Changing the
selected models resets the version selection; region and capacity filters stay
in place. For several models, version options include their model names.

Model/version choices remain paired internally and in the table. Selecting
model A/version 1 and model B/version 2 does not also select A/2 or B/1. Existing
links and saved views with paired selections restore their parent models.

Use **Table view** to browse model families, models, model/version choices, or
detailed deployment options. Select a table row or chart bar to drill from
family to model to version and then to the matching regional/subscription
options. Breadcrumbs return to a broader level while retaining your other
scope filters. The bar chart counts distinct catalog model/version choices
or regions, not a sum of incompatible quota units. **Hide chart** restores a
more compact table view.

The **System / Light / Dark** control follows the operating-system preference
by default. An explicit choice is saved in this browser and applies to every
dashboard view, including filters, charts, comparisons and collection settings.

**Changes** compares two dated snapshots. Additions and removals are evaluated
only in their common successfully observed subscription/region scope, so a
failed region does not look like a mass model removal. Different account-kind
entries with identical inventory data are combined, with the original kinds
retained in model details.

Data lives under the ignored `data` directory:

| Path | Purpose |
|---|---|
| `data\inventory.sqlite3` | SQLite snapshots and collection health |
| `data\config.json` | Local tenant/subscription selection |
| `data\runs` | Collection CSVs, coverage and diagnostic logs |

The newest **complete** snapshot is the default view. Partial and failed attempts
remain visible in collection history and do not silently replace it. Historical
snapshots are retained; there is no automatic deletion policy.

### Every-morning collection

Enable the morning schedule in **Collection & history** and choose a local time.
The initial time is **07:00**. The named Windows Task Scheduler job runs the same
collector independently of the browser or dashboard process:

```powershell
python -m dashboard schedule --enable --time 07:00
python -m dashboard schedule
python -m dashboard schedule --disable
```

The task runs as your current Windows user with limited privileges and no stored
password. You must be signed into Windows, and your Azure CLI sign-in must still
be valid. Missed runs use Task Scheduler's start-when-available behavior. If
authentication expires, run `az login --tenant "<tenant-guid>"` again; failures
are recorded rather than ingested as an empty estate. Overlapping scheduled and
manual collections are blocked.

The schedule collects data; it does not launch the browser or expose a web
server. Start the dashboard whenever you want to explore the latest data.

### Import an existing snapshot or use the command line

The combined CSV produced by the original inventory scripts can be imported
without contacting Azure:

```powershell
python -m dashboard import --csv "C:\path\to\all-subscriptions.csv"
```

Use `--csv` with several per-subscription CSV paths to ingest them as one estate
snapshot. The importer uses `ScanStartedUtc` when present; `--started-at` can
supply an explicit ISO 8601 time. Do not mix unrelated observation times into
one snapshot.

```powershell
python -m dashboard configure `
    --tenant-id "<tenant-guid>" `
    --subscription-id "<subscription-guid-1>" "<subscription-guid-2>"
python -m dashboard collect
```

Every command accepts `--data-dir` after its command name to use another local
data directory. Keep that directory private and outside tracked source.

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

The dashboard serves only its fixed static assets and API routes, never the
database, configuration files or arbitrary local paths. Collection and schedule
changes require a same-origin request and a per-process request token.
Subscriptions are supplied as validated identifiers; the browser cannot send
arbitrary shell commands. This is a single-user local application, not a
multi-user service: do not put it behind a public proxy or share its port.
Loopback is not user authentication; other local processes or users can reach
the service. Use a trusted workstation and protect the local data directory.

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
python -m unittest discover -s tests -p "test_dashboard_*.py"
```

The harness writes generated fixtures under the ignored `test-output` directory.
It covers paging, error responses, unknown and zero quota, shared quota pools,
retirement metadata, pricing units/tiers, and object output. Dashboard tests
use temporary SQLite databases and mocked collection/scheduling, not live
subscriptions or task registrations.

## API references

- [Model catalog](https://learn.microsoft.com/rest/api/aiservices/accountmanagement/models/list)
- [Quota and capacity](https://learn.microsoft.com/azure/foundry/openai/how-to/quota)
- [Azure Retail Prices API](https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices)
