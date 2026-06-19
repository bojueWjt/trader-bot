# contracts-v1

This package freezes the Hermes to Nautilus window A contract surface as
language-neutral JSON Schema draft 2020-12.

Window B should consume the canonical schemas directly from:

- `packages/contracts/v1/hermes_decision.v1.json`
- `packages/contracts/v1/approved_trade_intent.v1.json`
- `packages/contracts/v1/execution_event_envelope.v1.json`
- `packages/contracts/v1/system_snapshot.v1.json`

`packages/contracts/version.json` is the version manifest. `dist/index.json`
is a convenience index that maps each contract name to its `$id`, relative path,
and version.

## Compatibility rule

`CONTRACTS_VERSION` is `contracts-v1`, and every payload uses
`schema_version: "1.0"`.

After this baseline, breaking changes require a contract-change-request before
they land. Breaking changes include deleting a field, narrowing an enum,
narrowing a type, or adding a required field. The pytest suite compares the
current schema shape to `v1/.snapshot.json` and fails on those changes.

To intentionally re-baseline after an approved contract-change-request:

```bash
UPDATE_SNAPSHOT=1 python3 -m pytest tests/contracts -q
```

## Validation

Examples live under `v1/examples/valid` and `v1/examples/invalid`. The contract
tests validate the examples, check stable JSON round-tripping, enforce
`additionalProperties: false` on every object schema, and guard backward
compatibility.

The local Python environment may need:

```bash
python3 -m pip install jsonschema pytest
```

If dependency installation is unavailable, the committed tests include a minimal
standard-library validator for the subset of JSON Schema used by contracts-v1.

