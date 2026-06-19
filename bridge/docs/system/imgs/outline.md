---
type: framework
density: rich
style: sketch-notes
palette: warm
image_count: 4
---

## Illustration 1
**Position**: 系统总览
**Purpose**: 解释核心数据流
**Visual Content**: Telegram watcher, Hermes trader, Signal Store, Freqtrade, Dashboard, Reports
**Filename**: 01-architecture.svg

## Illustration 2
**Position**: 信号生命周期
**Purpose**: 展示状态机和幂等
**Visual Content**: raw, parsed, approved, reserved, sent_to_freqtrade, entered, partially_exited, exited
**Filename**: 02-lifecycle.svg

## Illustration 3
**Position**: 部署和运维
**Purpose**: 展示两台远端主机和 SSH importer bridge
**Visual Content**: balen.wang, Freqtrade server, LaunchAgents, PM2, docker services, root@149.104.30.223 runtime bridge, IPv6 operations entry
**Filename**: 03-deployment-topology.svg

## Illustration 4
**Position**: 门禁状态
**Purpose**: 展示 testnet_candidate 到 live gates 的推进关系
**Visual Content**: Local Bridge, Testnet Candidate, Live Readonly, Live Small Size
**Filename**: 04-gates.svg
