# .live-mirror — hk /srv/trader-v3 线上热补丁文件的部署源镜像

- `api/read_api.py`：线上控制面(uvicorn 127.0.0.1:8080)的部署源。线上与 git 主源码(services/control-plane)长期漂移(见 docs/plans/2026-07-14-attribution-review.md P1-11)，本目录是当前唯一可测试的线上真实版本。改动流程：改这里 → pytest → scp 到 hk(.bak 备份) → systemctl restart trader-v3-controlplane。
- `tests/`：FastAPI TestClient + 假 DB 的单测(2026-07-14 归属加固 Phase 1 起建)。
- `scripts/`、`skill/`：拉取时的参考镜像,不保证最新;CLI/SKILL 的部署源在 hermes-profile/。
