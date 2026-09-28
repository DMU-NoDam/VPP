"""메모리 데이터를 HTTP 로 내보낸다. 수집도 저장도 하지 않는다."""

from __future__ import annotations

import math

from fastapi import FastAPI, HTTPException

from domain import DATASET_BY_NAME, DATASETS, Tables


def create_app(tables: Tables) -> FastAPI:
    """tables 를 물고 있는 앱. 라우트는 메모리에서 읽어 응답한다."""
    app = FastAPI(title="collector data api", version="0.1.0")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "rows": {n: len(df) for n, df in tables.frames.items()}}

    @app.get("/datasets")
    def datasets() -> list[dict]:
        return [
            {
                "name": d.name,
                "columns": list(d.columns),
                "key": list(d.key),
                "time_column": d.time_column,
                "interval_sec": d.interval_sec,
                "rows": len(tables.frames.get(d.name, [])),
            }
            for d in DATASETS
        ]

    @app.get("/data/{name}")
    def data(name: str, start: str | None = None, end: str | None = None,
             limit: int | None = None) -> dict:
        if name not in DATASET_BY_NAME:
            raise HTTPException(status_code=404, detail=f"그런 데이터셋 없음: {name}")

        df = tables.get(name, start, end)
        if limit is not None:
            df = df.tail(limit)

        return {
            "dataset": name,
            "count": len(df),
            "rows": [_clean(r) for r in df.to_dict("records")],
        }

    return app


def _clean(row: dict) -> dict:
    """NaN 은 JSON 으로 못 내보내므로 None 으로 바꾼다."""
    return {
        k: (None if isinstance(v, float) and math.isnan(v) else v)
        for k, v in row.items()
    }
