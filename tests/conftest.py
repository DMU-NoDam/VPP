"""테스트 공용 설정.

서비스끼리는 import 하지 않지만(CLAUDE.md), 테스트는 대상 서비스의 모듈을
직접 불러와야 한다. dispatch_api 는 collector 와 같은 flat 모듈 관례를 쓰므로
그 디렉터리를 sys.path 에 넣어준다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DISPATCH_API_DIR = REPO_ROOT / "services" / "dispatch_api"

if str(DISPATCH_API_DIR) not in sys.path:
    sys.path.insert(0, str(DISPATCH_API_DIR))


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def csv_dir() -> Path:
    return REPO_ROOT / "data" / "csv"
