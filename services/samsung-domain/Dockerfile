FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /opt/samsung-domain
COPY pyproject.toml requirements.lock ./
COPY src ./src
RUN python -B -m pip install --no-cache-dir -r requirements.lock \
    && python -B -m pip install --no-cache-dir --no-deps . \
    && groupadd --gid 10001 domain \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin domain
USER 10001:10001
EXPOSE 8080 8443
ENTRYPOINT ["python", "-B", "-m", "samsung_domain.main"]
