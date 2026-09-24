# Foundry model inventory

Read-only PowerShell scripts and a lightweight localhost dashboard for Microsoft
Foundry model catalogs, deployment types, subscription quota, and snapshot history.
The standalone script also supports optional public retail pricing. An optional
[hosted, read-only copy](#hosted-dashboard-azure-app-service) runs the same code
in one Azure web app behind Microsoft Entra sign-in.

This repository contains code and synthetic tests only. It does not contain
tenant inventory, live subscription identifiers, reports, credentials, or Azure
authentication state.

## Requirements

- Windows PowerShell 5.1 or PowerShell 7.
- Python 3.11 or newer for the dashboard. It uses only the Python standard
  library: no package installation, frontend build, or database service.
- Azure CLI, signed in for each tenant you collect. Use a separate private
  profile per tenant when accounts or sign-ins differ.
- Subscription-level permissions to read Cognitive Services models and usages.
  Subscription Reader is sufficient for the inventory workflow; follow your
  organization's least-privilege policy. No write or deployment role is needed.
- Only for the optional hosted dashboard: an Azure subscription where you may
  create resources and role assignments, the GitHub CLI, and Docker if you want
  to build and test the container image locally.

These scripts use Azure's commercial-cloud management and pricing endpoints.
They do not change the default Azure CLI subscription or deploy resources;
only `scripts\deploy-hosted.ps1` deploys, and only when you run it.

## Local dashboard

Keep your checkout on a local drive, for example `D:\foundry-model-inventory`.
From that checkout:

```powershell
.\start-dashboard.ps1
```

Open **http://127.0.0.1:8765**. The server binds to loopback only and runs in the
foreground until you press Ctrl+C. Use `-Port` to choose another local port.
There are no external fonts, CDNs, analytics, or automatic browser calls to
Azure. Public pricing references are opened only when you choose their link.

### Double-click launcher (Windows)

If you prefer a click-to-open executable rather than starting PowerShell from
a terminal, build the optional launcher once from a local checkout with the
.NET 10 SDK:

```powershell
dotnet publish .\launcher\FoundryQuota.csproj -c Release -o .\data\launcher
```

Double-click **`data\launcher\FoundryQuota.exe`** in File Explorer. It opens a
separate Windows PowerShell window for the dashboard, waits until the local
server responds, then opens the default browser at
**http://127.0.0.1:8765/**. Leave the PowerShell window open to keep browsing;
press Ctrl+C there to stop the server. If the dashboard is already running on
that port, double-clicking opens it without starting a second copy. A different
service on the port is reported as an error, not opened in your browser. An
already-running server keeps its original process ownership; stop a dashboard
started in Copilot before launching it independently.

The executable is a launcher, not a bundled copy of your inventory, Python,
PowerShell, or Azure CLI. It needs the .NET 10 runtime on the machine that
runs it; the .NET SDK is needed only to build it. It uses the source and ignored
`data` directory of the checkout containing `data\launcher`, so it works
without a Copilot terminal and keeps local credentials out of the executable.
It does not update the checkout, run collection automatically, or change the
morning schedule. If the source is updated from `main`, the next launch uses
the updated checked-out code. Keep the executable in that checkout, not on
the Desktop by itself.

The friendly bookmark name **Foundry quota** can point to the loopback URL.
Names such as `foundryquota.127.com` are not inherently local and do not make
the dashboard accessible from other devices: `127.0.0.1` always means the
device using the URL. This local dashboard has **no user authentication** and
must not be exposed through public DNS, port forwarding, or an unprotected
tunnel. For remote access, use the separate
[hosted dashboard](#hosted-dashboard-azure-app-service), which adds HTTPS and
Microsoft Entra sign-in and is read-only.

In **Collection & history**, discover subscriptions from a signed-in Azure CLI
profile, choose a tenant and its subscriptions, and save the scope. Repeat with
a separate profile to add another tenant without replacing the first one.
**Collect now** scans every configured subscription into one read-only estate
snapshot. You can keep browsing the previous snapshot while it runs; the
dashboard warns if that snapshot does not yet include the current scope.

The inventory supports tenant, subscription, region, model family, exact model/version,
deployment geography, PAYG/PTU/batch, lifecycle, and remaining-quota filters.
Categorical filters are searchable, checkbox-based multi-selects: selections
within a filter are matched with **OR**, and different filters combine with
**AND**. Choose exactly one quota unit before entering a minimum, so a numeric
threshold cannot silently mix TPM and PTUs. Saved views stay in your browser.
Inventory browsing still works when browser storage is unavailable.
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

Use **View** to browse model families, models, model/version choices, or
detailed deployment options. Select a table row or chart bar to drill from
family to model to version and then to the matching regional/subscription
options. Breadcrumbs return to a broader level while retaining your other
scope filters. The bar chart counts distinct catalog model/version choices
or regions, not a sum of incompatible quota units. **Hide chart** restores a
more compact table view.

The default **Quota pools** view answers where quota is reported available.
Each named subscription/region/quota pool appears once, with an inline
allocated-versus-available bar and its original unit. Shared pools identify how
many catalog choices use the same allocation; choosing one model does not
invent a separate budget for it. Conflicting pool readings remain unknown even
if the conflicting model is outside the current filter. Unmapped entries remain
separate because their pool identity is not known.

Quota is ranked by availability, with amounts grouped by unit rather than
comparing PTUs to TPM. Rows whose matching catalog choices all need lifecycle or
SKU review are labeled accordingly. **Review matching models** opens the
existing catalog drill-down. Export from this view also emits each matching
pool once. Catalog views remain available in the same View selector; no second
quota chart repeats the table's amounts.

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
snapshots are retained; there is no automatic deletion policy. After adding or
removing subscriptions, the prior complete snapshot still reflects its original
scope until a new complete collection finishes.

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
password. You must be signed into Windows, and each tenant's selected Azure CLI
profile must still be valid. Missed runs use Task Scheduler's
start-when-available behavior. If authentication expires, sign in again to the
affected profile and tenant; failures are recorded rather than ingested as an
empty estate. Overlapping scheduled and manual collections are blocked.

If collection reports **does not exist in MSAL token cache**, Azure CLI still
has subscription metadata but no usable cached sign-in for the selected account.
Seeing the subscriptions in `az account list` does not confirm authentication.
Run `az login --tenant "<tenant-guid>"` interactively for the configured tenant,
as the same Windows user and with the same `AZURE_CONFIG_DIR` (if set) used by
the dashboard and scheduled task. Then choose **Collect now**. Do not clear
shared credentials or change the default subscription as a repair step.

The dashboard summarizes this failure once with sign-in guidance. Original
diagnostics remain in scan history and the local run logs; missing output files
are not reported as additional failures when the collector already failed.
The last complete snapshot remains selected until a new collection succeeds.
The dashboard never opens a sign-in flow automatically.

The schedule collects data; it does not launch the browser or expose a web
server. Start the dashboard whenever you want to explore the latest data.

### Isolated Azure CLI profiles and additional tenants

For multiple accounts or tenants, keep inventory authentication separate from
your shared Azure CLI profile. From the checkout, create a new private profile
under the ignored `data` directory and use normal browser sign-in:

```powershell
$profileDirectory = Join-Path $PWD "data\azure-cli"
if (Test-Path -LiteralPath $profileDirectory) {
    throw "This profile already exists; reuse it deliberately or choose another directory."
}
$null = New-Item -ItemType Directory -Path $profileDirectory
$env:AZURE_CONFIG_DIR = (Resolve-Path -LiteralPath $profileDirectory).Path
az config set core.enable_broker_on_windows=false
az config set core.login_experience_v2=off
az login --tenant "<tenant-guid>"
```

Only continue after login completes successfully. Persist the same profile for
the dashboard and scheduled collector:

```powershell
python -m dashboard configure `
    --tenant-id "<tenant-guid>" `
    --subscription-id "<subscription-guid-1>" "<subscription-guid-2>" `
    --azure-config-dir $env:AZURE_CONFIG_DIR
```

The absolute profile path is saved as `azure_config_dir` in `data\config.json`.
To add a different tenant, create another private directory under `data`, set
`AZURE_CONFIG_DIR` to that directory **in your sign-in terminal only**, and
run `az login --tenant "<additional-tenant-guid>"`. Verify an authorized read
before adding it:

```powershell
az provider show --namespace Microsoft.CognitiveServices `
    --subscription "<additional-subscription-guid>" `
    --query namespace --output tsv
```

In **Collection & history**, enter the new absolute profile directory, choose
**Discover subscriptions**, select the new tenant and its subscriptions, then
choose **Save tenant scope** and **Collect now**. The existing tenants stay
configured. Or use the command line after signing in to the new profile:

```powershell
python -m dashboard configure --add `
    --tenant-id "<additional-tenant-guid>" `
    --subscription-id "<additional-subscription-guid>" `
    --azure-config-dir $env:AZURE_CONFIG_DIR
python -m dashboard collect
```

The private configuration stores a `tenant_id` on each additional-tenant
subscription and its profile directory under `tenant_profiles`. Discovery,
manual collection, and the morning task use the appropriate profile for each
subscription, including after restarting the dashboard or logging in through
a different terminal. Saving the primary tenant's scope or the schedule does
not drop additional tenants. Profile paths must stay under this checkout's
ignored `data` directory; if one is unavailable, that subscription fails
explicitly instead of using the shared CLI credentials. Other subscriptions
that were observed successfully are retained as a **partial** attempt, and
the last complete snapshot remains selected. Protect these directories like
credentials, and never publish or force-add them to Git.

When `azure_config_dir` is absent, existing Azure CLI environment/default-profile
behavior is unchanged. To deliberately return to that behavior, stop collection
and remove that optional field from the local configuration; removing the field
does not delete the private profile or its credentials.

The local app remains a dependency-light **single-user** workbench. Anyone can
clone the code and configure their own tenants, subscriptions and local CLI
profiles, but they must already have read access to each subscription and
sign in to each tenant; configuration does not grant Azure access. The local
web server is not a per-user authenticated service; the separate hosted
dashboard is. Only catalog models and
quota reported by `Microsoft.CognitiveServices` appear: an absent Claude entry
does not prove zero quota or entitlement, and reported quota is not a promise
of deployment capacity.

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
directory under this checkout's ignored `data` directory, for example
`--data-dir .\data\archive`. Paths outside `data` are rejected before any files
are created. Keep all local data private and never force-add it to Git.

## Hosted dashboard (Azure App Service)

An optional, read-only copy of the dashboard for everyone in one Microsoft Entra
tenant. It runs `python -m dashboard hosted` in a single Linux container on Azure
App Service, collects the configured subscriptions every morning with its own
Reader-only managed identity, and shows only builds from `main`. It keeps the
project lightweight: Python standard library, no database server, no storage
account, and no stored Azure passwords.

### What gets created

`scripts\deploy-hosted.ps1` deploys `infra\main.bicep` (subscription scope) and
creates the sign-in app registration. `<suffix>` is a deterministic
`uniqueString` of the subscription and resource group.

| Resource | Name | Purpose |
|---|---|---|
| Resource group | `rg-foundry-inventory` | Holds everything; Central US unless `-Location` is given |
| Container registry, Basic | `acrfoundryinv<suffix>` | Private images; admin user disabled |
| App Service plan, B1 Linux | `asp-foundry-inventory-<suffix>` | One small instance |
| Web app | `app-foundry-inventory-<suffix>` | Custom container on port 8000, HTTPS only, TLS 1.2+, Always On, FTP and basic publishing credentials disabled |
| User-assigned identity | `id-foundry-inventory-deploy-<suffix>` | GitHub Actions on `main` signs in through OpenID Connect |
| Entra app registration and service principal | **Foundry inventory** | Single-tenant sign-in with ID tokens and no client secret |

The web app pulls its image with its system-assigned identity. App Service
authentication requires sign-in on every route and redirects browsers to
Microsoft Entra. Any member or guest of the tenant can sign in; to allow only
named people, turn on **Assignment required** for the **Foundry inventory**
enterprise application and assign them. The same container serves the dashboard
and runs the daily collection; there is no separate job service.

### Monthly cost

At public list prices (USD, Central US, September 2026): the B1 Linux plan is
$0.018 per hour, about **$13.14** for 730 hours, and a Basic registry is $0.1666
per day, about **$5.07** a month with 10 GB of storage included. The total is
about **$18 a month**. Managed identities, the app registration and role
assignments have no charge; GitHub-hosted runners are free for public
repositories. Each deployment adds an image tag, but unchanged layers are shared.
If registry storage approaches 10 GB, delete old `foundry-inventory` tags.

### Permissions granted

| Identity | Role | Scope |
|---|---|---|
| Web app, system-assigned | AcrPull | The registry |
| Web app, system-assigned | Reader | Each collected subscription |
| Deploy identity, user-assigned | AcrPush | The registry |
| Deploy identity, user-assigned | Website Contributor | The web app |

No person receives a new role. The deploy identity's federated credential trusts
only runs on `main` of this repository, from
`https://token.actions.githubusercontent.com`, so pull requests and other
branches cannot deploy. Its subject is the one GitHub presents for that branch:
`repo:<owner>@<owner-id>/<repo>@<repo-id>:ref:refs/heads/main` when the
repository uses immutable subject claims (the default for repositories created
since July 2026), otherwise `repo:<owner>/<repo>:ref:refs/heads/main`. The
script reads it from the repository's OIDC settings.

### Deploy

You need rights to create resources and role assignments (for example Owner) on
the hosting and collected subscriptions, permission to register applications in
Entra, a dedicated Azure CLI profile that is already signed in, and `gh` signed
in to read the repository's OIDC settings and, with `-SetGitHubSecrets`, set its
secrets. The script never signs in, signs out or changes the default
subscription, and refuses the default `~/.azure` profile.

```powershell
.\scripts\deploy-hosted.ps1 -AzureConfigDir .\data\azure-cli `
    -SubscriptionId "<hosting-subscription-guid>" -ValidateOnly
.\scripts\deploy-hosted.ps1 -AzureConfigDir .\data\azure-cli `
    -SubscriptionId "<hosting-subscription-guid>" -SetGitHubSecrets
```

The scope comes from `data\config.json`: its subscriptions in the hosting
subscription's tenant, and its morning time. Use `-CollectSubscriptionId` to
choose subscriptions explicitly, and `-MorningTime`, `-TimeZone`,
`-SnapshotRetentionDays`, `-Location` or `-ResourceGroupName` to override
defaults. `-ValidateOnly` registers the
`Microsoft.Web` provider when needed, validates the template and prints a
what-if summary without other changes. A full run creates or updates the app
registration and its service principal, deploys the template, sets the redirect
URI `https://<web-app-host>/.auth/login/aad/callback` from the deployed host
name, and, with `-SetGitHubSecrets`, stores `AZURE_CLIENT_ID`,
`AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP`,
`AZURE_WEBAPP_NAME` and `AZURE_ACR_NAME` as Actions secrets. It prints resource
names, never tokens.

Until the first build from `main`, the web app shows only App Service's default
page behind sign-in. Every push to `main` then runs
`.github/workflows/hosted-dashboard.yml`: tests on Windows, an offline container
smoke test, then a build pushed as `foundry-inventory:<commit>`. The web app is
pointed at exactly that tag, and the workflow checks that an unauthenticated
HTTPS request is redirected to Microsoft Entra sign-in. Deployments run one at a
time, and a run whose commit is no longer the tip of `main` leaves the web app
unchanged, so an older run that finishes late cannot replace a newer image. Pull
requests run the tests and the image smoke test without secrets.

### Redeploy, change and tear down

- **New code:** merge to `main`. To redeploy the current commit, re-run its
  workflow or use **Run workflow** on `main`.
- **Scope, schedule or infrastructure:** edit the local config or pass
  parameters, then run the script again. It is idempotent and keeps the image
  the workflow last deployed. The web app restarts with the new settings.
- **Repository rename, transfer or OIDC subject change:** run the script again
  so the federated credential matches the subject GitHub now presents. Until
  then, the deploy job fails at Azure sign-in with `AADSTS700213`.
- **Collect sooner:** collection runs daily at the configured local time. On
  start, the service also collects when no complete snapshot exists since the
  most recent scheduled time, so a restart retries a missed or failed collection.
- **Tear down:** remove the Reader assignments first, then the resources, the app
  registration and the secrets:

```powershell
$env:AZURE_CONFIG_DIR = (Resolve-Path .\data\azure-cli).Path
$principal = az webapp identity show --resource-group rg-foundry-inventory `
    --name "<web-app-name>" --subscription "<hosting-subscription-guid>" --query principalId --output tsv
foreach ($id in "<collected-subscription-guid-1>", "<collected-subscription-guid-2>") {
    az role assignment delete --assignee $principal --role Reader --scope "/subscriptions/$id"
}
az group delete --name rg-foundry-inventory --subscription "<hosting-subscription-guid>"
az ad app delete --id "<sign-in-app-client-id>"
"AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_ID", "AZURE_RESOURCE_GROUP",
    "AZURE_WEBAPP_NAME", "AZURE_ACR_NAME" | ForEach-Object { gh secret delete $_ --repo "<owner>/<repo>" }
```

Deleting the resource group removes the registry, plan, web app, deploy identity
and the role assignments inside it. Disable the workflow as well, or the next
push to `main` fails at Azure sign-in.

Role assignment names come from the resource IDs, not the identities. If the web
app, the deploy identity or the resource group was deleted before its role
assignments, delete the leftover assignments (the portal lists them as
**Identity not found**) on the collected subscriptions, the registry and the web
app before you run the script again. Otherwise Azure rejects the new identity
with `RoleAssignmentUpdateNotPermitted`.

### Data handling

- The live SQLite database stays on the container's local disk, because SQLite
  must not run on App Service's network-backed `/home` share. After each finished
  collection the service writes a consistent copy with the SQLite backup API to
  local staging, verifies it, then copies it to
  `/home/foundry-inventory/inventory.sqlite3` and renames it into place
  atomically. A new container without a local database restores that copy. An
  unreadable copy is kept aside, never overwritten.
- The collection scope is an app setting (`FOUNDRY_INVENTORY_SCOPE`), not part of
  the repository or image. The image contains code only.
- Managed-identity sign-in state lives only on the container disk, never in
  `/home`. The diagnostics of the latest seven collections are kept there.
- After each collection the service deletes snapshots older than 90 days
  (`-SnapshotRetentionDays`, 7 to 3650), always keeping the latest complete
  one, so the database and its `/home` copy stay bounded. A daily snapshot of
  three subscriptions is about 21 MB, so 90 days is about 2 GB. B1 includes
  10 GB of `/home` storage, and replacing the copy briefly needs twice the
  database size. **Collection & history** shows the latest persistent copy. The
  local dashboard keeps every snapshot.
- Container logs are kept in `/home/LogFiles` for three days (35 MB). There is no
  Application Insights resource, and Azure CLI and PowerShell telemetry are off.
- The server re-checks the App Service identity headers and the tenant on every
  request, accepts only the web app's host names, and refuses any change over
  HTTP. External requests cannot set those headers while authentication is on.

### Limits and follow-up

- One tenant. A managed identity reads only its own tenant, so configured
  subscriptions in other tenants (for example a separate Claude or partner
  tenant) are skipped and stay in the local dashboard. Collecting them from the
  hosted service would need cross-tenant app setup, such as a multi-tenant app
  registration that trusts the web app's identity, plus consent and Reader in
  the other tenant. That is a follow-up, not part of this deployment.
- The hosted dashboard is read-only: collection, scope, schedule and discovery
  are deployment settings, not browser actions.
- Guests sign in with the same user consent as members. If someone sees **Need
  admin approval**, a Cloud Application Administrator can grant consent once.

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

Model details also include a **Public unit pricing** reference to the Retail
Prices API. This API provides raw per-unit list rates independently of Cost
Management usage/spend data. The link sends only the public model name and region,
not tenant identifiers or credentials. Its meter search is explicitly unverified:
names can include other versions, regional/global types or fine-tuned variants.
The dashboard does not guess input/output/cache prices from those matches.

The dashboard serves only its fixed static assets and API routes, never the
database, configuration files or arbitrary local paths. Collection and schedule
changes require a same-origin request and a per-process request token.
Subscriptions are supplied as validated identifiers; the browser cannot send
arbitrary shell commands. This is a single-user local application, not a
multi-user service: do not put it behind a public proxy or share its port.
Loopback is not user authentication; other local processes or users can reach
the service. Use a trusted workstation and protect the local data directory.
The hosted mode is a separate, read-only server; see its section for its
authentication and data handling.

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
subscriptions or task registrations. Hosted-mode tests simulate App Service
identity headers and use fake clocks.

The container image has an offline smoke test that needs only Docker. It checks
sign-in enforcement, read-only routes, the bundled PowerShell and Azure CLI, and
that a new container restores the `/home` backup:

```powershell
docker build --build-arg GIT_COMMIT=0123456789abcdef -t foundry-inventory:local .
python scripts\smoke-test-hosted-image.py --image foundry-inventory:local --commit 0123456789abcdef
```

## API references

- [Model catalog](https://learn.microsoft.com/rest/api/aiservices/accountmanagement/models/list)
- [Quota and capacity](https://learn.microsoft.com/azure/foundry/openai/how-to/quota)
- [Azure Retail Prices API](https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices)
