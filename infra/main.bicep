// Hosted, read-only Foundry inventory: one Linux web app behind Microsoft Entra
// sign-in, pulling its image from a private registry with its managed identity.
// Deploy through scripts/deploy-hosted.ps1, which supplies the current image so a
// redeploy never resets what GitHub Actions last deployed from main.
targetScope = 'subscription'

@description('Region for the resource group and all resources.')
param location string = 'centralus'

@description('Resource group that holds every hosted resource.')
param resourceGroupName string = 'rg-foundry-inventory'

@description('Client ID of the single-tenant Microsoft Entra app registration used for sign-in.')
param authClientId string

@description('Collection scope JSON: tenant_id, subscriptions [{id, name}] and morning_time.')
param inventoryScope string

@description('Subscriptions the web app identity may read (Reader). Must match the collection scope.')
@minLength(1)
param readerSubscriptionIds array

@description('Image currently deployed, for example <registry>.azurecr.io/foundry-inventory:<commit>; empty before the first deployment from main.')
param containerImage string = ''

@description('OIDC subject prefix GitHub presents for the repository whose main branch may deploy: repo:<owner>@<owner-id>/<repo>@<repo-id> with immutable subject claims (the default for new repositories), otherwise repo:<owner>/<repo>.')
param githubSubjectPrefix string

@description('Branch allowed to deploy through the federated credential.')
param githubBranch string = 'main'

@description('IANA time zone for the daily collection time.')
param timeZone string = 'America/Chicago'

@description('Tags applied to the resource group and resources.')
param tags object = {
  workload: 'foundry-model-inventory'
  component: 'hosted-dashboard'
  managedBy: 'infra/main.bicep'
}

resource group 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module app 'modules/app.bicep' = {
  name: 'foundry-inventory-app'
  scope: group
  params: {
    location: location
    suffix: uniqueString(subscription().id, resourceGroupName)
    authClientId: authClientId
    inventoryScope: inventoryScope
    containerImage: containerImage
    githubSubjectPrefix: githubSubjectPrefix
    githubBranch: githubBranch
    timeZone: timeZone
    tags: tags
  }
}

module readers 'modules/subscription-reader.bicep' = [for subscriptionId in readerSubscriptionIds: {
  name: 'foundry-inventory-reader-${uniqueString(subscriptionId, resourceGroupName)}'
  scope: subscription(subscriptionId)
  params: {
    principalId: app.outputs.sitePrincipalId
    siteId: app.outputs.siteId
  }
}]

output resourceGroupName string = group.name
output registryName string = app.outputs.registryName
output registryLoginServer string = app.outputs.registryLoginServer
output planName string = app.outputs.planName
output siteName string = app.outputs.siteName
output siteHostName string = app.outputs.siteHostName
output sitePrincipalId string = app.outputs.sitePrincipalId
output deployIdentityName string = app.outputs.deployIdentityName
output deployClientId string = app.outputs.deployClientId
output deployPrincipalId string = app.outputs.deployPrincipalId
