# Examples

이 디렉터리는 사용자에게 노출되는 예제 진입점을 설명합니다. 현재 서울 아파트 실거래가 end-to-end 예제의 canonical config는 `scripts/configs/korean_apartment_trades.yaml`입니다.

Issue #50에는 `examples/seoul_apt_trade.yaml` 항목이 있었지만, 선행 PR #52에서 이미 publish script용 YAML이 추가되었기 때문에 같은 내용을 중복 생성하지 않습니다. 실행 문서는 `docs/examples/seoul-apt-trade.md`를 참고하세요.

## 서울 아파트 실거래가

로컬 파일만 생성하는 안전한 실행 예시는 다음과 같습니다.

```bash
uv run python scripts/publish_to_hf.py scripts/configs/korean_apartment_trades.yaml --local-only
```

업로드를 건너뛰는 dry-run 예시는 다음과 같습니다.

```bash
uv run python scripts/publish_to_hf.py scripts/configs/korean_apartment_trades.yaml --dry-run
```

생성되는 기본 출력 구조는 다음과 같습니다.

```text
staging/korean-apartment-trades/
├── README.md
└── data/
    └── train.parquet
```

`README.md`는 Hugging Face dataset card이며, `data/train.parquet`는 정제된 tabular dataset입니다. 실제 데이터 fetch에는 data.go.kr API key가 필요할 수 있으며, Hugging Face 업로드는 이 예제에서 수행하지 않습니다.
