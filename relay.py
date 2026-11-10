#!/usr/bin/env python3
"""BNT stratum TCP relay v3 -- hardened listen -> bntpool.com:3333

Keunggulan vs v2:
  * banyak port listen sekaligus (8443 + 443) biar aman kalau host klien blokir port
  * half-close benar: kalau satu sisi EOF, sisi lain di-SHUT_WR dulu bukan diputus,
    jadi share/submit terakhir gak kepotong
  * upstream di-resolve ulang tiap 60s (DNS bntpool berubah = relay gak mati)
  * retry upstream 4x dengan backoff, gak langsung buang klien
  * TCP keepalive + TCP_NODELAY di dua sisi, backlog 1024
  * idle reaper 900s (koneksi zombie dibuang, FD balik)
  * cap MAX_CONN dengan penolakan rapi
  * status HTTP JSON di :8444 (uptime, koneksi aktif, total, byte, upstream)
  * log satu baris per event, gak spam per-bit
"""
import json
import os
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler

UP_HOST = os.environ.get("RELAY_UPSTREAM_HOST", "bntpool.com")
UP_PORT = int(os.environ.get("RELAY_UPSTREAM_PORT", "3333"))
PORTS = [int(p) for p in os.environ.get("RELAY_PORTS", "8443").split(",") if p.strip()]
STATUS_PORT = int(os.environ.get("RELAY_STATUS_PORT", "8444"))
IDLE_LIMIT = float(os.environ.get("RELAY_IDLE", "900"))
MAX_CONN = int(os.environ.get("RELAY_MAX_CONN", "500"))
BUF = 65536
START = time.time()

_lock = threading.Lock()
stats = {
    "active": 0,
    "total_conns": 0,
    "rejected": 0,
    "upstream_fail": 0,
    "bytes_up": 0,     # klien -> pool
    "bytes_down": 0,   # pool -> klien
    "clients": {},
}
_up = {"ip": None, "resolved_at": 0}


def log(msg):
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# ---------- upstream DNS cache ----------
def resolve_upstream(force=False):
    now = time.time()
    if not force and _up["ip"] and now - _up["resolved_at"] < 60:
        return _up["ip"]
    try:
        infos = socket.getaddrinfo(UP_HOST, UP_PORT, socket.AF_INET, socket.SOCK_STREAM)
        ip = infos[0][4][0]
        if _up["ip"] and _up["ip"] != ip:
            log("UPSTREAM-DNS %s -> %s" % (_up["ip"], ip))
        _up["ip"] = ip
        _up["resolved_at"] = now
        return ip
    except Exception as e:
        log("UPSTREAM-DNS-FAIL %s" % e)
        return _up["ip"]


def tune(sock):
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for opt, val in (("TCP_KEEPIDLE", 120), ("TCP_KEEPINTVL", 60), ("TCP_KEEPCNT", 3)):
        if hasattr(socket, opt):
            try:
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)
            except OSError:
                pass


def connect_upstream():
    delay = 0.4
    for attempt in range(4):
        ip = resolve_upstream(force=(attempt > 0))
        if ip:
            try:
                s = socket.create_connection((ip, UP_PORT), timeout=8)
                tune(s)
                return s
            except Exception:
                pass
        time.sleep(delay)
        delay *= 2
    with _lock:
        stats["upstream_fail"] += 1
    return None


# ---------- one client <-> pool pipe ----------
class Pipe(threading.Thread):
    def __init__(self, client, addr):
        super().__init__(daemon=True)
        self.client = client
        self.addr = addr
        self.upstream = None
        self.t0 = time.time()
        self.up = 0
        self.down = 0
        self.done = threading.Event()
        self.tag = "%s:%s" % (addr[0], addr[1])

    def run(self):
        self.upstream = connect_upstream()
        if not self.upstream:
            log("REJECT %s upstream-unreachable" % self.tag)
            self._close()
            return
        log("OPEN %s -> %s:%s" % (self.tag, _up["ip"], UP_PORT))
        t_up = threading.Thread(target=self._pump, args=(self.client, self.upstream, "up"), daemon=True)
        t_dn = threading.Thread(target=self._pump, args=(self.upstream, self.client, "down"), daemon=True)
        t_up.start()
        t_dn.start()
        t_up.join()
        t_dn.join()
        dur = time.time() - self.t0
        log("CLOSE %s %.0fs up=%dB down=%dB" % (self.tag, dur, self.up, self.down))
        self._close()

    def _pump(self, src, dst, kind):
        try:
            while not self.done.is_set():
                src.settimeout(IDLE_LIMIT)
                chunk = src.recv(BUF)
                if not chunk:
                    break
                dst.sendall(chunk)
                with _lock:
                    if kind == "up":
                        self.up += len(chunk)
                        stats["bytes_up"] += len(chunk)
                    else:
                        self.down += len(chunk)
                        stats["bytes_down"] += len(chunk)
        except socket.timeout:
            log("IDLE %s %s-side" % (self.tag, kind))
        except OSError:
            pass
        finally:
            self.done.set()
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def _close(self):
        for s in (self.client, self.upstream):
            try:
                s and s.close()
            except OSError:
                pass
        with _lock:
            stats["active"] = max(0, stats["active"] - 1)
            stats["clients"].pop(self.tag, None)


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        with _lock:
            over = stats["active"] >= MAX_CONN
            if not over:
                stats["active"] += 1
                stats["total_conns"] += 1
                stats["clients"][self.client_address[0]] = stats["clients"].get(self.client_address[0], 0) + 1
            else:
                stats["rejected"] += 1
        if over:
            log("REJECT %s conn-cap" % (self.client_address,))
            try:
                self.request.close()
            except OSError:
                pass
            return
        tune(self.request)
        Pipe(self.request, self.client_address).run()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 1024


# ---------- status HTTP ----------
class Status(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        body = json.dumps({
            "relay": "bnt-relay v3",
            "uptime_s": int(time.time() - START),
            "listen_ports": PORTS,
            "upstream": "%s (%s):%s" % (UP_HOST, _up["ip"], UP_PORT),
            "active": stats["active"],
            "total_conns": stats["total_conns"],
            "rejected": stats["rejected"],
            "upstream_fail": stats["upstream_fail"],
            "bytes_up": stats["bytes_up"],
            "bytes_down": stats["bytes_down"],
            "clients": stats["clients"],
        }, indent=2).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def upstream_watchdog():
    while True:
        resolve_upstream(force=True)
        time.sleep(60)


def main():
    resolve_upstream(force=True)
    log("bnt-relay v3 ports=%s -> %s:%s (ip %s) status=:%s" % (PORTS, UP_HOST, UP_PORT, _up["ip"], STATUS_PORT))
    threading.Thread(target=upstream_watchdog, daemon=True).start()
    for p in PORTS:
        srv = Server(("0.0.0.0", p), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log("LISTEN 0.0.0.0:%s" % p)
    try:
        st = socketserver.ThreadingTCPServer(("0.0.0.0", STATUS_PORT), Status)
        st.daemon_threads = True
        st.allow_reuse_address = True
        threading.Thread(target=st.serve_forever, daemon=True).start()
        log("STATUS 0.0.0.0:%s" % STATUS_PORT)
    except OSError as e:
        log("STATUS-FAIL %s" % e)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()