FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# main.py 가 같은 폴더의 domain/store/collector/data_api/sources 를 가져다 쓴다.
COPY . .

EXPOSE 8000

CMD ["python", "-u", "main.py"]
