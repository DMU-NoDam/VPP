FROM python:3.11-slim

WORKDIR /app

# 빌드 컨텍스트는 레포 루트다 (shared/ 스키마를 같이 넣기 위해). docker-compose.yml 참고.
COPY services/dispatch_api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ shared/
COPY services/dispatch_api/*.py .

EXPOSE 8002

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8002"]
