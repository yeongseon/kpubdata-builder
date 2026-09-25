#!/usr/bin/env bash
# app-01 rollout — deploy.yml 이 SSH stdin 으로 흘려보내 실행한다.
#
# 이 스크립트는 secret 을 인자로 받지 않는다. 호출자가 같은 stdin 앞부분에
# 환경변수 대입문을 실어 보내므로, 값이 원격 호스트의 프로세스 목록(`ps`)에
# 나타나지 않는다. 커맨드라인으로 넘기던 시절에는 app-01 의 모든 사용자가
# API 키와 credential master key 를 읽을 수 있었다.
set -euo pipefail

echo "[app-01] connected at $(date -u +%FT%TZ); host=$(hostname); deploying sha=${BUILDER_TAG}"
cd ~/kpubdata-builder
git fetch "https://x-access-token:${GH_TOKEN}@github.com/${GH_REPO}.git" main
git reset --hard FETCH_HEAD

# VM-local .env 렌더 (chmod 600). committed compose는 ${VAR} 참조만 사용.
umask 077
cat > .env <<EOF
BUILDER_IMAGE=ghcr.io/${REPO_OWNER}/kpubdata-builder:${BUILDER_TAG}
KPUBDATA_BUILDER_API_KEY=${KPUBDATA_BUILDER_API_KEY}
KPUBDATA_BUILDER_ALLOWED_ORIGINS=${KPUBDATA_BUILDER_ALLOWED_ORIGINS}
KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY=${KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY}
APP_DOMAIN=${APP_DOMAIN}
EOF

echo "[app-01] logging in to GHCR as ${GHCR_USER}"
echo "${GHCR_TOKEN}" | docker login ghcr.io -u "${GHCR_USER}" --password-stdin
echo "[app-01] pulling builder image (BUILDER_TAG=${BUILDER_TAG})"
docker compose -f docker-compose.prod.app.yml pull builder </dev/null

# APP_DOMAIN 이 있으면 Caddy 프로파일까지 띄운다. 예전에는 builder 만 띄우면서
# 러너가 http://HOST:8000 을 직접 찔렀는데, 그 조합은 배포가 성공하려면 8000 을
# 공개해야 한다는 뜻이고 — TLS 없이 API 키가 평문으로 오간다.
compose_args=(-f docker-compose.prod.app.yml)

# 이미지가 비루트(uid 10001)로 돌기 시작했다. named volume 은 컨테이너보다 오래
# 살고 예전 배포에서 root 소유로 만들어졌으므로, 그대로 두면 새 이미지가 /data 에
# 쓰지 못한다. 소유자가 이미 맞으면 아무 일도 하지 않는다(멱등).
#
# compose 를 통해 실행한다 — 볼륨 이름에는 compose 프로젝트 접두사가 붙고 그
# 접두사는 디렉터리 이름에서 오므로, 이름을 직접 적으면 배포 경로가 바뀔 때 조용히
# 빗나간다.
echo "[app-01] ensuring the data volume is writable by uid 10001"
docker compose "${compose_args[@]}" run --rm --no-deps --user root \
  --entrypoint sh builder -c \
  'if [ "$(stat -c %u /data)" != "10001" ]; then chown -R 10001:10001 /data; echo "  chowned"; else echo "  already correct"; fi' \
  </dev/null || echo "[app-01] warning: could not adjust volume ownership; the first run may fail"
if [ -n "${APP_DOMAIN:-}" ]; then
  compose_args+=(--profile caddy)
  echo "[app-01] starting builder + caddy (APP_DOMAIN=${APP_DOMAIN})"
else
  echo "[app-01] APP_DOMAIN unset — starting builder only (IP-only deployment)"
fi
docker compose "${compose_args[@]}" up -d </dev/null
docker compose "${compose_args[@]}" ps </dev/null || true

# 헬스 체크는 VM 안에서 한다. 러너에서 공개 포트를 찌르는 방식은 그 포트가
# 열려 있어야만 배포가 성공하므로, 프로브가 곧 노출 요구가 된다.
echo "[app-01] probing /healthz from inside the VM (max 30 x 5s)"
for i in $(seq 1 30); do
  if docker compose "${compose_args[@]}" exec -T builder \
      python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz', timeout=3).status == 200 else 1)" </dev/null; then
    echo "[app-01] builder /healthz: OK (attempt ${i})"
    break
  fi
  if [ "${i}" -eq 30 ]; then
    echo "[app-01] builder did not become healthy after 30 attempts" >&2
    docker compose "${compose_args[@]}" logs --tail 80 builder </dev/null || true
    exit 1
  fi
  sleep 5
done
echo "[app-01] === END $(date -u +%FT%TZ) ==="
