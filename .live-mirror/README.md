# .live-mirror - Frozen Production Evidence

Effective date: 2026-08-09

- `services/` is the canonical application source.
- `.live-mirror/` preserves historical production bytes and supports drift
  comparison.
- Files in this directory are read-only evidence. They are excluded from build,
  deployment, hotpatch, and release source inventories.
- Production captures must record UTC time, container/image identity, source
  path, SHA-256, and the evidence cutoff.
- Drift checks compare canonical source against the frozen mirror and fail when
  a release attempts to consume mirror bytes.
- Historical mirror tests may replay old production behavior. Passing those
  tests does not authorize mirror deployment.

The previous workflow edited `api/read_api.py` here, copied it to the host, and
restarted the control plane. That workflow created two source authorities and
is retired. Production changes now require a reviewed immutable release from
canonical source.
