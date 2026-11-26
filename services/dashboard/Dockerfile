FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# app.py 가 같은 폴더의 logic.py 를 가져다 쓴다. 설정은 compose 가 /app/config 로 마운트.
# *.html: 로딩 화면·Two-Stage 3D 장면, *.json: 발전원 조합 VSS 측정 결과 (scripts/measure_vss_mix.py)
COPY *.py *.html *.json ./
# vendor/: three.js (Two-Stage 3D 를 인터넷 없이 띄우려고 HTML 에 넣는다)
COPY vendor ./vendor

EXPOSE 8501

CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
