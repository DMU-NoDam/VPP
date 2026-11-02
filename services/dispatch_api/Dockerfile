FROM python:3.11-slim

WORKDIR /app

# 빌드 컨텍스트는 레포 루트다 (shared/ 스키마를 같이 넣기 위해). docker-compose.yml 참고.
COPY services/dispatch_api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ shared/
# 서비스 디렉터리 전체를 넣는다. *.py 글롭만 쓰면 loaders/ 같은 하위 패키지가
# 빠져 컨테이너에서만 ImportError 가 난다 (__pycache__ 는 .dockerignore 가 막는다).
COPY services/dispatch_api/ .

EXPOSE 8002

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8002"]
