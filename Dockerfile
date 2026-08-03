# syntax=docker/dockerfile:1

# KPubData Builder — serve 배포 이미지 (#320, ADR 0006).
#
# uv sync --no-sources: [tool.uv.sources]의 editable ../kpubdata 오버라이드를 무시하고
# pyproject의 PyPI 핀(kpubdata>=0.5.0,<0.6, #213)대로 kpubdata를 설치한다. 진입점은
# kpubdata-builder serve이며, 환경변수로 설정을 주입한다 (docker-entrypoint.sh).
#
# ADR 0006 결정: 컨테이너는 fail-closed로 동작한다. KPUBDATA_BUILDER_API_KEY 없이는
# 기동하지 않는다 (docker-entrypoint.sh에서 강제). 베이스는 pragmatic한 python-slim
# (ADR-0006 미해결 질문: distroless 대안은 후속).

FROM python:3.12-slim

# uv 바이너리를 Astral 공식 이미지에서 복사한다 (pip 설치 불필요).
# 0.11.8 = 이 저장소의 uv.lock을 생성한 uv 버전.
COPY --from=ghcr.io/astral-sh/uv:0.11.8 /uv /uvx /bin/

# 가상환경을 고정 경로에 두고, 컴파일된 바이트코드와 함께 이미지 레이어로 캐싱.
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH=/app/.venv/bin:${PATH}

WORKDIR /app

# 매니페스트를 먼저 복사해 의존성 레이어를 캐시한다 (소스 변경 시에도 재사용).
COPY pyproject.toml uv.lock ./
COPY src/ ./src/
COPY README.md LICENSE ./

# --no-sources: editable ../kpubdata 무시, PyPI 핀 사용 (#213).
# dev extra(mypy/pytest/ruff)는 배포 이미지에서 제외한다.
RUN uv sync --no-sources

# 빌드 산출물(아티팩트·매니페스트) 영속 볼륨의 기본 위치.
RUN mkdir -p /data
VOLUME /data

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# KPUBDATA_BUILDER_PORT(기본 8000)가 이 포트를 가리킨다.
EXPOSE 8000

ENTRYPOINT ["docker-entrypoint.sh"]
