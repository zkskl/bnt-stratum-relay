# bnt-stratum-relay

Self-hosted stratum TCP relay untuk mining Blocknet (BNT) via [seine](https://github.com/zpalmtree/seine). Relay ini cuma pipa TCP: client miner -> relay -> pool (bntpool.com:3333).

Dipake pas egress VPS/miner kamu ke pool diblokir (TCP ke bntpool.com:3333 gak nyampe), tapi masih bisa keluar ke relay sendiri.

```
[miner] --stratum--> [relay:443/8443/...] --TCP--> [bntpool.com:3333]
```

## Kenapa relay?

- Pool cuma terima koneksi stratum dari IP yang bisa reach bntpool.com:3333 langsung
- Beberapa host (panel, claw, VPS trial) egress-nya ke port 3333 diblokir/di-filter
- Relay di VPS kamu sendiri = satu titik masuk yang boleh dipake banyak worker

## Fitur relay (v3)

- Listen di banyak port sekaligus (`8443,443,8445-8448`) — kalau satu port diblokir host klien, worker tetap masuk lewat port lain
- Half-close benar: share/submit terakhir gak kepotong
- Upstream DNS di-resolve ulang tiap 60s (bntpool ganti IP = relay gak mati)
- Retry upstream 4x dengan backoff
- TCP keepalive + TCP_NODELAY dua sisi, backlog 1024
- Idle reaper 900s (koneksi zombie dibuang)
- Cap MAX_CONN dengan penolakan rapi
- Status HTTP JSON di `:8444` (uptime, koneksi aktif, total, byte, upstream)
- Log satu baris per event

## Setup relay

Prereq: VPS Linux dengan python3 (stdlib only, gak ada dependency lain) + port TCP yang bisa diakses publik.

```bash
# 1. Letakkan relay.py di server (misal /root/bnt-relay/)
mkdir -p /root/bnt-relay
# copy relay.py ke /root/bnt-relay/relay.py

# 2. Install unit systemd (ganti PORT_LIST sesuai port yang mau dibuka)
cat > /etc/systemd/system/bnt-relay.service <<'EOF'
[Unit]
Description=BNT stratum TCP relay
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=RELAY_PORTS=443,8443
Environment=RELAY_STATUS_PORT=8444
ExecStart=/usr/bin/python3 /root/bnt-relay/relay.py
Restart=always
RestartSec=3
StandardOutput=append:/root/bnt-relay/relay.log
StandardError=append:/root/bnt-relay/relay.log

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now bnt-relay
```

Verifikasi:

```bash
systemctl is-active bnt-relay           # active
curl -s http://127.0.0.1:8444/          # JSON status
tail -5 /root/bnt-relay/relay.log
```

## Setup miner (client)

Di mesin miner (root):

```bash
ADDR=<YOUR_93_CHAR_BNT_ADDRESS>         # ganti address BNT kamu
RELAY=<YOUR_RELAY_IP>                   # ganti IP VPS relay

mkdir -p /root/bnt-seine-runner && cd /root/bnt-seine-runner
curl -L https://github.com/zpalmtree/seine/releases/download/v0.2.45/seine-linux-x86_64.tar.gz -o seine.tar.gz
tar -xzf seine.tar.gz && chmod +x ./seine && ./seine --version

./seine --mode pool \
  --pool-url "stratum+tcp://$RELAY:443" \
  --pool-worker <NAMA_WORKER> \
  --address "$ADDR" \
  --backend cpu --threads 1 --disable-cpu-autotune-threads \
  --ui plain --data-dir /root/bnt-seine-runner/data
```

Auto-restart pakai systemd:

```bash
cat > /etc/systemd/system/bnt-miner.service <<'EOF'
[Unit]
Description=Seine BNT miner via relay
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/root/bnt-seine-runner
ExecStart=/root/bnt-seine-runner/seine --mode pool --pool-url stratum+tcp://YOUR_RELAY_IP:443 --pool-worker YOUR_WORKER --address YOUR_93_CHAR_BNT_ADDRESS --backend cpu --threads 1 --disable-cpu-autotune-threads --ui plain --data-dir /root/bnt-seine-runner/data
Restart=always
RestartSec=15
Nice=15
LimitNOFILE=65536
StandardOutput=append:/root/bnt-seine-runner/miner.log
StandardError=append:/root/bnt-seine-runner/miner.log

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload && systemctl enable --now bnt-miner
journalctl -u bnt-miner -n 40 --no-pager | grep -Ei "login|new job|accepted|error"
```

## Healthcheck (opsional)

`healthcheck.sh` mengecek tiap 2 menit: TCP accept di port relay + status JSON `upstream_fail`/`rejected`. Dua kegagalan beruntun = restart unit. Log-only (gak notify siapa pun).

```bash
cp healthcheck.sh /root/bnt-relay/healthcheck.sh
chmod +x /root/bnt-relay/healthcheck.sh

cat > /etc/systemd/system/bnt-relay-health.service <<'EOF'
[Unit]
Description=BNT relay healthcheck

[Service]
Type=oneshot
ExecStart=/root/bnt-relay/healthcheck.sh
EOF

cat > /etc/systemd/system/bnt-relay-health.timer <<'EOF'
[Unit]
Description=BNT relay healthcheck timer

[Timer]
OnBootSec=2min
OnUnitActiveSec=2min

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now bnt-relay-health.timer
```

Catatan: `mining.subscribe` handshake BUKAN probe liveness — bntpool merespons "unknown method" pada frame buatan sendiri bahkan saat relay sehat. Healthcheck ini gak pakai stratum probe, cuma TCP accept + counter status.

## Troubleshooting

| Gejala | Penyebab | Cek |
|---|---|---|
| Worker connect tapi 0 byte | Relay mati di tengah | `systemctl is-active bnt-relay` |
| `upstream_fail` naik terus | Egress relay ke bntpool diblokir | `curl -s http://127.0.0.1:8444/` |
| `Address already in use` | Unit lama masih megang port | `ss -tlnp \| grep :443`, kill orphan |
| Worker gak muncul di pool | Butuh ~75s sampai share pertama accepted | `curl -s https://bntpool.com/api/miner/<addr>` |

## Disclaimer

- Relay cuma meneruskan traffic, gak pegang private key / address payout
- Pool: bntpool.com (fee 0%, PPLNS 6h, min payout 0.1 BNT). Dev fee seine 2.5% (1.0% kalau upstream hostname = bntpool.com — lewat relay hostname jadi IP relay, fee naik ke 2.5%; itu harga dari relay).
- Gunakan bijak. Jangan abuse VPS/panel orang lain.