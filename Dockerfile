# Hosted, read-only Foundry inventory for Azure App Service (Linux, custom container).
# The image contains code only: no inventory, configuration, credentials or test output.
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=America/Chicago \
    AZURE_CORE_COLLECT_TELEMETRY=false \
    POWERSHELL_TELEMETRY_OPTOUT=1 \
    POWERSHELL_UPDATECHECK=Off

# PowerShell 7 and Azure CLI come from Microsoft's signed Debian repositories;
# tzdata lets TZ select the local collection time.
RUN set -eux; \
    export DEBIAN_FRONTEND=noninteractive; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg tzdata; \
    install -d -m 0755 /etc/apt/keyrings; \
    curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /etc/apt/keyrings/microsoft.gpg; \
    chmod 0644 /etc/apt/keyrings/microsoft.gpg; \
    printf '%s\n' 'Types: deb' 'URIs: https://packages.microsoft.com/debian/12/prod' \
        'Suites: bookworm' 'Components: main' 'Architectures: amd64' \
        'Signed-By: /etc/apt/keyrings/microsoft.gpg' > /etc/apt/sources.list.d/microsoft-prod.sources; \
    printf '%s\n' 'Types: deb' 'URIs: https://packages.microsoft.com/repos/azure-cli/' \
        'Suites: bookworm' 'Components: main' 'Architectures: amd64' \
        'Signed-By: /etc/apt/keyrings/microsoft.gpg' > /etc/apt/sources.list.d/azure-cli.sources; \
    apt-get update; \
    apt-get install -y --no-install-recommends powershell azure-cli; \
    apt-get purge -y --auto-remove curl gnupg; \
    rm -rf /var/lib/apt/lists/*; \
    pwsh -NoLogo -NoProfile -NonInteractive -Command '$PSVersionTable.PSVersion.ToString()'; \
    AZURE_CONFIG_DIR=/tmp/azure-cli-build az version --output none; \
    rm -rf /tmp/azure-cli-build /root/.cache /root/.local

WORKDIR /app
COPY check-foundry-model-availability.ps1 run-foundry-subscription-inventory.ps1 ./
COPY dashboard/ dashboard/

# App Service mounts /home as root-owned persistent storage; the service keeps
# its live database on the container disk and writes verified copies there.
# The commit is declared last so it never invalidates the cached tool layer.
ARG GIT_COMMIT=unknown
ENV FOUNDRY_INVENTORY_COMMIT=${GIT_COMMIT}
LABEL org.opencontainers.image.title="Foundry model inventory (hosted)" \
      org.opencontainers.image.revision="${GIT_COMMIT}"
EXPOSE 8000
CMD ["python", "-m", "dashboard", "hosted"]
