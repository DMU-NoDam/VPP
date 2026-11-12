FROM python:3.11-slim

WORKDIR /app

# LightGBM 은 OpenMP 런타임(libgomp)이 있어야 import 된다. slim 이미지에는 없어서 설치.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# 수요·풍력 LSTM 예측에 torch가 필요하다. 기본 빌드는 GPU용이라 수 GB여서 CPU 전용을 먼저 설치한다.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# main.py가 같은 폴더의 router/schemas/forecast_engine/data_client 등을 가져다 쓴다.
# 학습된 모델(models/saved/*)도 같이 들어간다. venv 등은 .dockerignore로 제외.
COPY . .

EXPOSE 8001

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
