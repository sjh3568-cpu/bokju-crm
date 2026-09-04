# 복주 상담실 CRM — 시놀로지 NAS Container Manager 배포용
FROM python:3.12-slim

# 컨테이너 기본 시간대는 UTC라 그대로 두면 상담일·통계 집계가 9시간 어긋난다.
ENV TZ=Asia/Seoul \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py auth.py backup.py config.py llm.py models.py serve.py ./
COPY templates/ ./templates/
COPY static/ ./static/
COPY tools/ ./tools/
COPY sms.py ./

# DB와 백업은 반드시 마운트 볼륨에 둔다 — 컨테이너를 지우거나 이미지를 새로
# 올려도 데이터가 남아야 한다.
ENV BOKJU_DB_PATH=/data/bokju.db \
    BACKUP_DIR=/backups
RUN mkdir -p /data /backups

EXPOSE 8003
CMD ["python", "serve.py"]
