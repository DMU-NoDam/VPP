"""각 API 서비스의 /health 엔드포인트 스모크 테스트.

서비스끼리는 import하지 않으므로, 테스트에서만 파일 경로로 직접 로드한다.
"""

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SERVICES_DIR = Path(__file__).resolve().parents[1] / "services"


def load_app(service_name: str):
    module_name = f"{service_name}_main"
    path = SERVICES_DIR / service_name / "main.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module.app


@pytest.mark.parametrize("service_name", ["forecast_api", "dispatch_api"])
def test_health(service_name: str) -> None:
    client = TestClient(load_app(service_name))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
