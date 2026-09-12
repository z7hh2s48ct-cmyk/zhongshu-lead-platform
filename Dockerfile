FROM python:3.12-slim-bookworm@sha256:72d3d75f2639ab82b34b29390ad3d6e0827c775befee94edda8e9976818f488d
ARG APP_VERSION=1.2.5
LABEL org.opencontainers.image.title="zhongshu-lead-platform" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.description="合家美宅客资审核、派发与积分管理平台"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN useradd --create-home --uid 10001 appuser
COPY requirements.txt requirements-postgres.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-postgres.txt
# 刷新 Debian 安全仓库并升级基础镜像已安装的 PCRE2；生产仍只允许 PostgreSQL。
RUN apt-get update \
    && apt-get install -y --no-install-recommends --only-upgrade libpcre2-8-0 \
    && pcre2_version="$(dpkg-query -W -f='${Version}' libpcre2-8-0)" \
    && dpkg --compare-versions "$pcre2_version" ge "10.42-1+deb12u1" \
    && apt-get purge -y libsqlite3-0 \
    && rm -rf /var/lib/apt/lists/*
COPY . .
RUN chmod +x docker/*.sh scripts/*.py && mkdir -p /app/storage && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3)"
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["uvicorn","apps.api.src.main:app","--host","0.0.0.0","--port","8000","--proxy-headers"]
