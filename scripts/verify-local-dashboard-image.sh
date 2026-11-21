#!/bin/sh
# Run the local dashboard profile in isolation, without an ingress or gateway credentials.
set -eu
image=${1:?Usage: sh scripts/verify-local-dashboard-image.sh IMAGE}
docker run --pull=never --rm -i --read-only --tmpfs /tmp --network none \
  --entrypoint /bin/sh "$image" -eu <<'CHECK'
nginx -t
nginx
check() {
  expected=$1
  request_host=$2
  path=$3
  shift 3
  actual=$(curl --silent --show-error --output /dev/null --write-out '%{http_code} %{redirect_url}' \
    -H "Host: $request_host" -H "Accept: $accept" "$@" "http://127.0.0.1$path")
  if [ "$actual" != "$expected" ]; then
    echo "$request_host$path: expected [$expected], received [$actual]" >&2
    exit 1
  fi
  echo "$request_host$path: $actual"
}
host=liveprobe.localhost
accept=text/html
check '307 http://127.0.0.1:4175/' "$host" /
check '307 http://127.0.0.1:4175/incidents/1501?step=evidence&tab=flow' "$host" '/incidents/1501?step=evidence&tab=flow' --head
check '307 http://127.0.0.1:4175/application?incident=app_42' "$host" '/application?incident=app_42'
check '200 ' "$host" '/incidents/1501' -H 'X-LiveProbe-Local-View: dashboard'
accept=application/json check '200 ' "$host" /
check '200 ' liveprobe.example.invalid '/incidents/1501'
check '404 ' "$host" /api
check '404 ' "$host" /api/unified/catalog
check '404 ' "$host" /api/actions --request POST
check '405 ' "$host" /incidents/1501 --request POST
set -- /usr/share/nginx/html/assets/*.js
check '200 ' "$host" "/assets/${1##*/}"
check '404 ' "$host" /assets/missing.js
CHECK
