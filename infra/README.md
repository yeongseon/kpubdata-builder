# KPubData Builder — Azure 배포 (Bicep)

Azure Container Apps + Azure Files + Log Analytics 최소 배포 템플릿.

## 전제

- **단일 replica** (`minReplicas: 1, maxReplicas: 1`) — ADR 0010(#375) 상태 백엔드가 로컬 FS/SQLite라 replica 확장 불가.
- Builder는 **internal ingress** (공개 노출 없음, ADR 0009).
- API 키는 시크릿으로 주입 (fail-closed).

## 배포

```bash
# 리소스 그룹 생성 (처음 1회)
az group create --name kpubdata-builder-rg --location koreacentral

# 배포
az deployment group create \
  --resource-group kpubdata-builder-rg \
  --template-file infra/main.bicep \
  --parameters \
    imageName=ghcr.io/yeongseon/kpubdata-builder:latest \
    apiKey=$(az keyvault secret show --vault-name <kv> --name builder-api-key --query value -o tsv) \
    allowedOrigins=https://studio.example.com \
    containerCpu=1.0 \
    containerMemory=2Gi \
    builderMaxWorkers=4 \
    queryMaxConcurrency=1
```

`main.bicep`의 보수적 기본값은 단일 replica에 `1.0` vCPU/`2Gi`, HTTP worker 4개,
query child 1개다. 애플리케이션 자체 기본값(HTTP 10, query 2)보다 작게 명시하여 작은
ACA 인스턴스의 process/thread 과다 경쟁을 피한다. 비동기 build pool은 현재 코드 기본값
10개로 고정되어 있으며 이 Bicep parameter의 영향을 받지 않는다. 산정과 튜닝 절차는
[`docs/deploy.md`](../docs/deploy.md#9-리소스-예산과-튜닝)를 따른다.

## 리소스

| 리소스 | 용도 |
| :--- | :--- |
| Storage Account + File Share | `/data` 영속 볼륨 (산출물 + BuildIndex) |
| Log Analytics Workspace | 구조화 로그 수집 (request_id 추적, #379) |
| Container Apps Environment | ACA 실행 환경 |
| Container App | Builder serve 컨테이너 |

## 제약

- Azure Files 위 SQLite는 파일 잠금 불안정 — ADR 0010 이행 전까지 replica 1 고정.
- GHCR 이미지가 필요 (#376 — 토큰 `workflow` 스코프 대기 중).
