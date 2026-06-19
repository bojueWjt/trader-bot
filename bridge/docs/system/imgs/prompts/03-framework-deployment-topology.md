---
type: framework
style: sketch-notes
palette: warm
filename: 03-deployment-topology.svg
language: zh
---

# Prompt

Create a deployment topology diagram for two hosts. Use sketch-notes style, cream paper, black connector arrows, and pastel host boxes.

Host A:
- balen.wang / dalongxia.local
- Hermes gateway LaunchAgent
- Hermes trader LaunchAgent
- OpenClaw gateway LaunchAgent
- telegram-watcher PM2

Host B:
- qctbhy0a32hucub
- 149.104.30.223
- 2001:df1:7880:2::18dc
- Docker freqtrade-dryrun on 127.0.0.1:18081
- Docker frontend on 127.0.0.1:13000
- Signal Store SQLite

Connector:
- watcher importer SSH bridge
- current runtime target root@149.104.30.223
- operations target ssh -6 root@2001:df1:7880:2::18dc

Aspect: wide 16:9.
