#!/bin/sh
# KPubData Builder 컨테이너 진입점 (#320, ADR 0006).
#
# 환경변수를 `serve` CLI 플래그로 변환한다. 핵심은 ADR 0006의 fail-closed 정책을
# 컨테이너 경계에서 강제하는 것 — service/app.py의 "키 미설정 = 인증 생략" 동작은
# 로컬 개발 편의 전용이며 컨테이너로 누출되지 않아야 한다. 따라서 API 키가 없으면
# (명시적 dev 플래그가 없는 한) 기동을 거부한다.
#
# dev-mode 플래그 이름은 service/app.py:_is_dev_mode()가 읽는
# KPUBDATA_BUILDER_DEV_MODE와 동일해야 한다 (#371).
set -eu

# app.py의 _is_dev_mode()와 동일하게 'true'/'1'을 대소문자 무관으로 받는다.
case "${KPUBDATA_BUILDER_DEV_MODE:-}" in
  [Tt][Rr][Uu][Ee] | 1) _dev_mode=1 ;;
  *) _dev_mode=0 ;;
esac

if [ -z "${KPUBDATA_BUILDER_API_KEY:-}" ] && [ "$_dev_mode" != "1" ]; then
  echo "kpubdata-builder: KPUBDATA_BUILDER_API_KEY is required (fail-closed, ADR 0006)." >&2
  echo "  set KPUBDATA_BUILDER_API_KEY=<secret>, or KPUBDATA_BUILDER_DEV_MODE=1 for" >&2
  echo "  unauthenticated local use." >&2
  exit 1
fi

exec kpubdata-builder serve \
  --host "${KPUBDATA_BUILDER_HOST:-0.0.0.0}" \
  --port "${KPUBDATA_BUILDER_PORT:-8000}" \
  --output-dir "${KPUBDATA_BUILDER_OUTPUT_DIR:-/data}"
