// KPubData Builder — ACA 배포 (Azure Container Apps)
// #378: 최소 IaC. 단일 replica 전제 (ADR 0010 상태 백엔드 미해결 전까지).
//
// 배포 전제:
//   - 리소스 그룹 + Log Analytics는 별도 생성 또는 기존 것 사용
//   - Azure Files 공유를 /data 볼륨으로 마운트
//   - API 키는 Key Vault 참조로 주입
//
// 사용:
//   az deployment group create \
//     --resource-group <rg> \
//     --template-file infra/main.bicep \
//     --parameters \
//       imageName=ghcr.io/yeongseon/kpubdata-builder:latest \
//       apiKey=<secret> \
//       azureFilesShareName=builder-data

@description('Container image to deploy.')
param imageName string = 'ghcr.io/yeongseon/kpubdata-builder:latest'

@description('Builder API key (X-API-Key). Required — fail-closed if empty.')
@secure()
param apiKey string

@description('Log Analytics workspace name (for diagnostics).')
param logAnalyticsWorkspaceName string = 'kpubdata-builder-logs'

@description('Azure Files share name for /data volume.')
param azureFilesShareName string = 'builder-data'

@description('Storage account name for Azure Files.')
param storageAccountName string = 'kpubdatabuilder'

@description('Allowed CORS origins (comma-separated).')
param allowedOrigins string = ''

@description('Location for all resources.')
param location string = resourceGroup().location

// --- Storage (Azure Files) ---
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
  }
}

resource fileShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: storageAccount
  name: azureFilesShareName
  properties: {
    shareQuota: 50
  }
}

// --- Log Analytics ---
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsWorkspaceName
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

// --- Container Apps Environment ---
resource cae 'Microsoft.App/managedEnvironments@2023-05-01' = {
  name: 'kpubdata-builder-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

// --- Container App ---
resource containerApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: 'kpubdata-builder'
  location: location
  properties: {
    managedEnvironmentId: cae.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: false
        targetPort: 8000
        transport: 'http'
        allowInsecure: false
      }
      secrets: [
        {
          name: 'api-key'
          value: apiKey
        }
      ]
    }
    template: {
      // ADR 0010 (#375): 상태 백엔드가 로컬 FS/SQLite라 replica 1 고정.
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
      containers: [
        {
          name: 'builder'
          image: imageName
          env: [
            {
              name: 'KPUBDATA_BUILDER_API_KEY'
              secretRef: 'api-key'
            }
            {
              name: 'KPUBDATA_BUILDER_OUTPUT_DIR'
              value: '/data'
            }
            {
              name: 'KPUBDATA_BUILDER_ALLOWED_ORIGINS'
              value: allowedOrigins
            }
          ]
          volumeMounts: [
            {
              volumeName: 'data'
              mountPath: '/data'
            }
          ]
        }
      ]
      volumes: [
        {
          name: 'data'
          storageType: 'AzureFile'
          storageName: azureFilesShareName
        }
      ]
    }
  }
}

output fqdn string = containerApp.properties.configuration.ingress.fqdn
output logAnalyticsWorkspaceId string = logAnalytics.id
