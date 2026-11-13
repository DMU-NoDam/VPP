# 탄소 최적화 검증 기록

측정일: 2026-11-13. 수집된 시간별 수요를 사용한 모델 재계산이며 실제 배출 감축 실적이 아니다.

## 재현 조건

- 실행: `.venv/bin/python scripts/measure_carbon_kpi.py --windows 3 --points 3 --json /tmp/vpp-carbon-kpi.json`
- 자정부터 연속 24시간인 최신 3개 창, ESS cost 모드, 부하차단 금지, 상대 MIP gap=0.
- 각 창에서 동일 수요·설비·예비율·ESS 조건의 비용 최소 계획과 비교.
- 계수: `config/carbon.yaml`의 급전 가능 발전원 전과정 범위 중간값. 무연탄은 석탄 계수 대용.
- 연료비는 설정의 latest 기준이며 과거 날짜 당시의 운영 재현이 아니다. 직접 수요 입력 경로의 저수율은 미지정이므로 수력 감률은 기본 1이다.
- 시간은 이 로컬 환경의 측정값이며 모든 입력에서의 API 응답시간 보장이 아니다.

## 측정 결과

| 수요 날짜 | 감축 목표 | 계산 감축률 | 운영비 증가 | 석탄 발전량 감소(MWh) | 제약 |
|---|---:|---:|---:|---:|---|
| 2026-10-09 | 5% | 5.00% | 5.36% | 65,089.7 | 통과 |
| 2026-10-09 | 10% | 10.00% | 12.04% | 134,799.0 | 통과 |
| 2026-10-06 | 5% | 5.00% | 5.17% | 69,307.5 | 통과 |
| 2026-10-06 | 10% | 10.00% | 11.69% | 142,471.5 | 통과 |
| 2026-10-05 | 5% | 5.00% | 6.05% | 63,106.6 | 통과 |
| 2026-10-05 | 10% | 10.00% | 12.93% | 127,666.8 | 통과 |

각 창의 전체 분석 시간: 2026-10-09 8.33초, 2026-10-06 5.48초, 2026-10-05 5.82초.

## 테스트

- 전체 회귀: 467 passed, 서비스 coverage 93%.
- 이후 상대 gap=0 전달을 명시하도록 수정하고 관련 회귀를 재실행: 178 passed.
- `ruff check .`, `git diff --check` 통과.
- macOS 테스트 시 LightGBM의 OpenMP 로딩에 기존 가상환경의 sklearn 번들 라이브러리 경로를 사용:
  `DYLD_LIBRARY_PATH=$PWD/.venv/lib/python3.12/site-packages/sklearn/.dylibs .venv/bin/python -m pytest -q`.
- 화면/API 실패 시 목업을 반환하지 않는 것, 탄소 상한, LNG 대체, 비용 회계, ESS SOC, 불가능 목표, 시간 예산, 미확보 해 처리 검증.

## 입력 파일 SHA-256

- `config/carbon.yaml`: `9e9bc9db7e46f14f6214a24a6d9aae8cb3426311d40d70fe9e94472601b05037`
- `config/generators.yaml`: `aa2e434e52855043c92371752727682d42b868d5e142aa9a197be6005e3ff7c8`
- `config/ess.yaml`: `54a952ac9aeb2212c783f835bbcb24d93acd62f924bb18b42de04db224a71116`
- `config/settings.yaml`: `572e4fb0e98e031b226689e2c4afaea84e4188270540e0011bc75f43a738ee9c`
- `data/csv/fuel_cost.csv`: `38b3d8d2429d34be23551fe2182b033d59a25dd602da06312f3aba3e6d9dd22b`
