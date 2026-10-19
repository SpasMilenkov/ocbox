#!/bin/sh
# ocbox container entrypoint. Runs as the unprivileged agent user.
# Makes "localhost:PORT" inside the sandbox reach the service bridge, so the project's
# normal dev-mode configuration (localhost:5432, localhost:5672, ...) works unchanged.
if [ -n "${OCBOX_FORWARD_PORTS:-}" ] && [ -n "${OCBOX_BRIDGE_IP:-}" ]; then
  for p in $(echo "$OCBOX_FORWARD_PORTS" | tr ',' ' '); do
    socat "TCP-LISTEN:${p},bind=127.0.0.1,fork,reuseaddr" "TCP:${OCBOX_BRIDGE_IP}:${p}" \
      >/dev/null 2>&1 &
  done
fi
exec "$@"
