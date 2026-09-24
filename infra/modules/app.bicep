// Registry, plan, web app with App Service authentication, deploy identity and
// the role assignments that stay inside the resource group.
@description('Region for all resources.')
param location string

@description('Deterministic suffix that keeps global names unique.')
param suffix string

@description('Client ID of the single-tenant Microsoft Entra app registration used for sign-in.')
param authClientId string

@description('Collection scope JSON for FOUNDRY_INVENTORY_SCOPE.')
param inventoryScope string

@description('Image currently deployed; empty before the first deployment from main.')
param containerImage string

@description('OIDC subject prefix GitHub presents for the deploying repository: repo:<owner>@<owner-id>/<repo>@<repo-id> with immutable subject claims, otherwise repo:<owner>/<repo>.')
param githubSubjectPrefix string

@description('Branch allowed to deploy.')
param githubBranch string

@description('IANA time zone for the daily collection.')
param timeZone string

param tags object

var roles = {
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  acrPush: '8311e382-0749-4cb8-b61a-304f252e45ec'
  websiteContributor: 'de139f84-1756-47ae-9be6-808fbbe84772'
}
var tenantId = tenant().tenantId

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'acrfoundryinv${suffix}'
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: 'asp-foundry-inventory-${suffix}'
  location: location
  tags: tags
  kind: 'linux'
  sku: {
    name: 'B1'
    tier: 'Basic'
  }
  properties: {
    reserved: true
  }
}

resource site 'Microsoft.Web/sites@2024-04-01' = {
  name: 'app-foundry-inventory-${suffix}'
  location: location
  tags: tags
  kind: 'app,linux,container'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    clientAffinityEnabled: false
    publicNetworkAccess: 'Enabled'
    siteConfig: {
      linuxFxVersion: empty(containerImage) ? '' : 'DOCKER|${containerImage}'
      acrUseManagedIdentityCreds: true
      alwaysOn: true
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      scmMinTlsVersion: '1.2'
      http20Enabled: true
      appSettings: [
        {
          name: 'WEBSITES_PORT'
          value: '8000'
        }
        {
          name: 'WEBSITES_ENABLE_APP_SERVICE_STORAGE'
          value: 'true'
        }
        {
          name: 'TZ'
          value: timeZone
        }
        {
          name: 'FOUNDRY_INVENTORY_AUTH_TENANT_ID'
          value: tenantId
        }
        {
          name: 'FOUNDRY_INVENTORY_SCOPE'
          value: inventoryScope
        }
        {
          // Platform-level tenant pin in addition to the single-tenant issuer.
          name: 'WEBSITE_AUTH_AAD_ALLOWED_TENANTS'
          value: tenantId
        }
      ]
    }
  }
}

resource ftpPublishing 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-04-01' = {
  parent: site
  name: 'ftp'
  properties: {
    allow: false
  }
}

resource scmPublishing 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-04-01' = {
  parent: site
  name: 'scm'
  properties: {
    allow: false
  }
}

resource authentication 'Microsoft.Web/sites/config@2024-04-01' = {
  parent: site
  name: 'authsettingsV2'
  properties: {
    platform: {
      enabled: true
      runtimeVersion: '~1'
    }
    globalValidation: {
      requireAuthentication: true
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'azureactivedirectory'
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          // No client secret: App Service uses the ID token (implicit) sign-in flow.
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenantId}/v2.0'
          clientId: authClientId
        }
        login: {
          disableWWWAuthenticate: false
        }
      }
    }
    login: {
      tokenStore: {
        enabled: true
      }
      preserveUrlFragmentsForLogins: false
    }
    httpSettings: {
      requireHttps: true
      forwardProxy: {
        convention: 'NoProxy'
      }
    }
  }
}

resource logging 'Microsoft.Web/sites/config@2024-04-01' = {
  parent: site
  name: 'logs'
  properties: {
    httpLogs: {
      fileSystem: {
        enabled: true
        retentionInDays: 3
        retentionInMb: 35
      }
    }
    detailedErrorMessages: {
      enabled: false
    }
    failedRequestsTracing: {
      enabled: false
    }
  }
}

resource deployIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-foundry-inventory-deploy-${suffix}'
  location: location
  tags: tags
}

resource githubFederation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: deployIdentity
  name: 'github-${githubBranch}'
  properties: {
    issuer: 'https://token.actions.githubusercontent.com'
    subject: '${githubSubjectPrefix}:ref:refs/heads/${githubBranch}'
    audiences: [
      'api://AzureADTokenExchange'
    ]
  }
}

resource siteAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, site.id, roles.acrPull)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
    principalId: site.identity.principalId
    principalType: 'ServicePrincipal'
    description: 'Foundry inventory web app pulls its image'
  }
}

resource deployAcrPush 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, deployIdentity.id, roles.acrPush)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPush)
    principalId: deployIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    description: 'GitHub Actions on main pushes images'
  }
}

resource deployWebsiteContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: site
  name: guid(site.id, deployIdentity.id, roles.websiteContributor)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.websiteContributor)
    principalId: deployIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    description: 'GitHub Actions on main points the web app at a new image'
  }
}

output registryName string = registry.name
output registryLoginServer string = registry.properties.loginServer
output planName string = plan.name
output siteName string = site.name
output siteId string = site.id
output siteHostName string = site.properties.defaultHostName
output sitePrincipalId string = site.identity.principalId
output deployIdentityName string = deployIdentity.name
output deployClientId string = deployIdentity.properties.clientId
output deployPrincipalId string = deployIdentity.properties.principalId
