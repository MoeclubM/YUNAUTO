FROM python:3.12-slim

# 时区设为上海，日志时间更直观
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY monitor.py .

# 账号密码通过环境变量传入：YUN_USERNAME / YUN_PASSWORD
CMD ["python", "monitor.py"]
