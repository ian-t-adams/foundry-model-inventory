// Read-only access for the web app's system-assigned identity on one subscription.
targetScope = 'subscription'

@description('Object ID of the web app system-assigned identity.')
param principalId string

@description('Resource ID of the web app, used to derive a stable assignment name.')
param siteId string

var readerRoleId = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'

resource reader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(subscription().id, siteId, readerRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', readerRoleId)
    principalId: principalId
    principalType: 'ServicePrincipal'
    description: 'Foundry inventory hosted dashboard: read-only collection'
  }
}
