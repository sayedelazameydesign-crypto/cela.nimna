#!/bin/sh
# nimna-vnc-guard — entrypoint wrapper for the `desktop` compose service.
#
# Refuses to start the VNC desktop when VNC_PASSWORD is missing, a known weak
# default (including the old hard-coded "nimna"), or shorter than 12 chars,
# then hands over to the image's real entrypoint (default: /startup.sh).
#
# Note: classic VNC (RFB) auth only uses the first 8 characters of the
# password, so the desktop port is also bound to 127.0.0.1 in compose.
set -eu

pw="${VNC_PASSWORD:-}"
lower=$(printf '%s' "$pw" | tr '[:upper:]' '[:lower:]')

case "$lower" in
  ""|nimna|password|passw0rd|changeme|change-me|change_me|secret|vnc|vncpassword|ubuntu|admin|root|123456|12345678|qwerty)
    echo "nimna-vnc-guard: VNC_PASSWORD is missing or a known weak default — refusing to start." >&2
    echo "nimna-vnc-guard: set a unique VNC_PASSWORD (>= 12 chars) in .env — see .env.example." >&2
    exit 64
    ;;
esac

if [ "${#pw}" -lt 12 ]; then
  echo "nimna-vnc-guard: VNC_PASSWORD must be at least 12 characters — refusing to start." >&2
  exit 64
fi

[ "$#" -gt 0 ] || set -- /startup.sh
exec "$@"
