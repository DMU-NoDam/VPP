"""CSV 와 메모리 사이.

`Tables` 와 디스크 사이만 오간다. API 는 모른다.
쓰기는 append 하나뿐이다. 전량 재작성은 하지 않는다.
어디까지 썼는지는 파일 마지막 줄의 시간으로 판단한다. 중복 판정은 `Tables` 몫이다.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from domain import DATASETS, Dataset, Tables

TAIL_BYTES = 4096  # 마지막 줄을 읽으려고 파일 끝에서 떼어보는 크기


def load(csv_dir: Path) -> Tables:
    """csv_dir 의 데이터셋 CSV 를 전량 읽어 메모리에 올린다."""
    frames = {}
    for ds in DATASETS:
        df = pd.read_csv(csv_dir / ds.filename, dtype={c: str for c in ds.key})
        frames[ds.name] = _tidy(ds, df)
    return Tables(frames=frames)


def save(csv_dir: Path, tables: Tables) -> None:
    """메모리에는 있고 파일에는 없는 행을 append 한다.

    파일은 시간순 append 전용이라 마지막 줄의 시간이 곧 어디까지 썼는지다.
    그보다 뒤인 행만 새로 적는다.
    """
    for ds in DATASETS:
        path = csv_dir / ds.filename
        written_until = _last_time(path, ds)

        df = tables.frames[ds.name]
        fresh = df[df[ds.time_column] > written_until]
        if fresh.empty:
            continue

        with path.open("a", newline="", encoding="utf-8") as f:
            fresh.to_csv(f, header=False, index=False, columns=list(ds.columns))


def _last_time(path: Path, ds: Dataset) -> str:
    """파일 마지막 줄의 시간 컬럼 값."""
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - TAIL_BYTES))
        lines = [line for line in f.read().decode("utf-8").splitlines() if line.strip()]

    return lines[-1].split(",")[ds.columns.index(ds.time_column)]


def _tidy(ds: Dataset, df: pd.DataFrame) -> pd.DataFrame:
    """컬럼을 정의대로 맞추고, key 중복을 정리하고, 순서를 세운다."""
    df = df.reindex(columns=list(ds.columns))
    df = df.drop_duplicates(subset=list(ds.key), keep="last")
    return df.sort_values(list(ds.key), kind="stable").reset_index(drop=True)
