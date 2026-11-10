#!/bin/sh
# Silent health check for bnt-relay on vps-athena.
#
# What it checks (NOT a stratum probe - bntpool ignores hand-rolled
# mining.subscribe frames, so that method gives false alarms):
#   1. TCP accept on every relay port (8443, 443)  -> listener alive
#   2. relay v3 status endpoint on :8444            -> JSON valid,
#      upstream_fail must not have grown, rejected must stay low
#
# A relay that accepts TCP but returns zero bytes (the failure mode that
# killed the DO relay v1) shows up as a growing upstream_fail / refused
# upstream in the status JSON, so this catches it too.
#
# Two consecutive failures required before restarting. Log only - it never
# notifies anybody.

STATE=/root/bnt-relay/.health_fails
LASTUF=/root/bnt-relay/.health_lastuf
LOG=/root/bnt-relay/health.log
PORTS="8443 443"
STATUS=http://127.0.0.1:8444/

tcp_ok() {
    timeout 5 python3 -c "
import socket,sys
s=socket.socket(); s.settimeout(4)
try: s.connect(('127.0.0.1',$1)); sys.exit(0)
except Exception: sys.exit(1)
finally: s.close()
" 2>/dev/null
}

ok=1
for p in $PORTS; do
    tcp_ok "$p" || { ok=0; why="port $p refuse"; }
done

# status endpoint + upstream health
st=$(timeout 8 python3 -c "
import json,urllib.request
try:
    d=json.load(urllib.request.urlopen('$STATUS',timeout=6))
except Exception as e:
    print('ERR'); raise SystemExit
print(int(d.get('upstream_fail',0)), int(d.get('rejected',0)))
" 2>/dev/null)

case "$st" in
    ""|ERR) ok=0; why="status endpoint mati" ;;
    *)
        uf=$(echo "$st" | cut -d' ' -f1)
        rj=$(echo "$st" | cut -d' ' -f2)
        prev=$(cat "$LASTUF" 2>/dev/null || echo "$uf")
        echo "$uf" > "$LASTUF"
        if [ "$uf" -gt "$prev" ]; then ok=0; why="upstream_fail naik $prev->$uf"; fi
        if [ "$rj" -gt 50 ]; then ok=0; why="rejected $rj"; fi
        ;;
esac

if [ "$ok" -eq 1 ]; then
    echo 0 > "$STATE"
    exit 0
fi

fails=$(cat "$STATE" 2>/dev/null || echo 0)
fails=$((fails+1))
echo "$fails" > "$STATE"
echo "$(date -Is) FAILED ($fails/2): $why" >> "$LOG"

if [ "$fails" -ge 2 ]; then
    echo "$(date -Is) restarting bnt-relay.service" >> "$LOG"
    systemctl restart bnt-relay.service
    echo 0 > "$STATE"
    sleep 10
    for p in $PORTS; do
        if tcp_ok "$p"; then echo "$(date -Is) recovered on :$p" >> "$LOG"; break; fi
    done
fi
exit 0
