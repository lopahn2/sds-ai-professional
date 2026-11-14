FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    SYSTEM_NAME=payment SERVICE_NAME=payment APP_ENV=dev APP_VERSION=p18-dev SELF_TRAFFIC=1
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin app
COPY app.py runtime.py /app/
COPY static/ /app/static/
USER 10001:10001
EXPOSE 8080
CMD ["python", "/app/app.py"]
