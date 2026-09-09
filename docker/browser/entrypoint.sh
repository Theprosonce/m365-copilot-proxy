#!/bin/sh
set -eu

export DISPLAY=:99
mkdir -p /data/profile
# Stale Chromium singleton locks can remain in the persisted profile after unclean exits
# and cause startup loops in containers whose hostname changes across restarts.
rm -f /data/profile/SingletonLock /data/profile/SingletonCookie /data/profile/SingletonSocket
find /data/profile -maxdepth 1 -name 'Singleton*' -type f -delete

Xvfb "$DISPLAY" -screen 0 "${SCREEN_SIZE:-1440x900x24}" -nolisten tcp -ac &
for _ in $(seq 1 50); do
  [ -S /tmp/.X11-unix/X99 ] && break
  sleep 0.1
done
[ -S /tmp/.X11-unix/X99 ] || { echo "Xvfb did not start on $DISPLAY" >&2; exit 1; }

x11vnc -display "$DISPLAY" -forever -shared -nopw -rfbport 5900 -localhost &
websockify --web=/usr/share/novnc 0.0.0.0:6080 localhost:5900 &
# Chromium's DevTools endpoint may remain loopback-only in containers.
# Provide a stable host-reachable TCP proxy on 0.0.0.0:9222.
python3 - <<'PY' &
import socket
import threading

LISTEN = ("0.0.0.0", 9222)
TARGET = ("127.0.0.1", 9223)


def pump(src, dst):
    try:
        while True:
            chunk = src.recv(65536)
            if not chunk:
                break
            dst.sendall(chunk)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def handle(client):
    try:
        upstream = socket.create_connection(TARGET, timeout=5)
    except OSError:
        client.close()
        return
    t1 = threading.Thread(target=pump, args=(client, upstream), daemon=True)
    t2 = threading.Thread(target=pump, args=(upstream, client), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    client.close()
    upstream.close()


with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(LISTEN)
    server.listen(128)
    while True:
        conn, _ = server.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()
PY

exec chromium \
  --no-sandbox \
  --disable-dev-shm-usage \
  --disable-gpu \
  --no-first-run \
  --start-maximized \
  --user-data-dir=/data/profile \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9223 \
  "${START_URL:-https://m365.cloud.microsoft/chat}"
