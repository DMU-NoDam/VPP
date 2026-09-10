# VPP 전력수급 최적화 에이전트

## 프로젝트 소개

TODO

## 아키텍처 다이어그램

TODO: 이미지 삽입

```
collector ──▶ data/  ◀── forecast_api (8001)  ◀── REST ──┐
                     ◀── dispatch_api (8002) ◀── REST ──┤
                                                  dashboard (8501)
```

TODO

> 서비스 간에는 서로의 코드를 import하지 않으며, 통신은 REST(HTTP)로만 한다.

## 실행 방법

```bash
cp .env.example .env
docker-compose up --build
```

| 서비스 | 포트 | 주소 |
| --- | --- | --- |
| forecast_api | 8001 | http://localhost:8001/health |
| dispatch_api | 8002 | http://localhost:8002/health |
| dashboard | 8501 | http://localhost:8501 |
| collector | - | 백그라운드 루프 |

TODO

## 팀원별 기여

| 이름 | 담당 모듈 | 역할 |
| --- | --- | --- |
|  |  |  |
|  |  |  |
|  |  |  |
|  |  |  |

TODO
