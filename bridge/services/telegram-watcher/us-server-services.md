# US Server Services

Host: 192.220.14.2
SSH: root@192.220.14.2 -p 39749

## Shadowsocks for Clash/Mihomo

Service: shadowsocks-rust.service
Config: /etc/shadowsocks-rust/config.json

Clash proxy node:

```yaml
- name: US-192.220.14.2-SS2022
  type: ss
  server: 192.220.14.2
  port: 49125
  cipher: 2022-blake3-aes-256-gcm
  password: "1C+B0DgLlJgRgEuwScOVPLulDcAI+jglu1thXqKz34Q="
  udp: true
```

## CLIProxyAPI

Service: cli-proxy-api.service
Config: /etc/cli-proxy-api/config.yaml
Base URL: http://192.220.14.2:8317

API key:

```text
cpa_5gVig5oW762A7_yiM6b9TPszWUqc5dOj
```

Management key:

```text
mgmt_1K-uZeXeRD0McllphW5QedJysAWV5iw5
```

OpenAI-compatible example:

```bash
curl http://192.220.14.2:8317/v1/models \
  -H 'Authorization: Bearer cpa_5g...dOj'
```

Last verified: 2026-05-11. New IP 192.220.14.2; TCP ports 49125, 8317 and 18317 reachable externally. CLIProxyAPI /v1/models returned {"data":[],"object":"list"} because no upstream accounts/API keys are configured yet.

## CLIProxyAPI Management Panel + Usage Statistics

Management panel URL:
http://192.220.14.2:18317/management.html

CPA upstream:
http://192.220.14.2:8317

Services:
- cli-proxy-api.service -> /etc/cli-proxy-api/config.yaml -> port 8317
- cpa-manager.service -> /var/lib/cpa-manager/usage.sqlite -> port 18317

Usage/statistics persistence:
- CLIProxyAPI usage publishing enabled: usage-statistics-enabled: true
- Queue retention: redis-usage-queue-retention-seconds: 3600
- SQLite stats DB: /var/lib/cpa-manager/usage.sqlite
- Service status endpoint: http://192.220.14.2:18317/status

Secrets:
- CLIProxyAPI Bearer API key: [REDACTED]
- Management key: [REDACTED]

## LAN Clash Verge Subscription

Local LAN subscription URL:
http://192.168.31.149:8790/us-proxy.yaml

Local file served:
/Users/balen/clash-sub/us-proxy.yaml

Local server command:
```bash
cd /Users/balen/clash-sub && python3 -m http.server 8790 --bind 0.0.0.0
```

Notes:
- This subscription is available only while the Mac is on the same LAN and the Python HTTP server is running.
- Clash node secret is inside the YAML file, not repeated here.
