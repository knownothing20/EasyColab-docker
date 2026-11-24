# 基础镜像可覆盖：本地若有内网镜像源，用 --build-arg BASE_IMAGE=xxx 复用，避免重复拉取
ARG BASE_IMAGE=python:3.13-slim
FROM ${BASE_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    EASYCOLAB_DATA_DIR=/data \
    EASYCOLAB_REPO_DIR=/opt/easycolab \
    EASYCOLAB_OUTPUT_DIR=/data/outputs

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime

COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app

RUN mkdir -p /data/outputs /opt/easycolab

EXPOSE 8626

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8626/api/health',timeout=4).read()" || exit 1

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8626", "--no-access-log"]