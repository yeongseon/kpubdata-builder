# ADR 0001 — 오케스트레이터로서의 Builder

## 결정
`kpubdata-builder`를 `kpubdata`의 대체물이 아니라 오케스트레이션 계층으로 취급한다.

## 근거
소스 접근을 `kpubdata`에 유지하면 중복을 피할 수 있고 책임 경계도 명확해진다.
