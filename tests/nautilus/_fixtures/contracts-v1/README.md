# contracts-v1 frozen fixtures (mirror)

These JSON Schemas mirror the **frozen contracts** defined in
`hermes-nautilus-migration-v3` PLAN v3.0 §2.2 and §3.

- **Authoritative source:** `packages/contracts/**` (owned by **window A**).
- **This copy:** a read-only mirror used by **window B** contract tests so the
  execution layer and dashboard can be built and validated independently of
  window A's branch. Window B must not diverge from these shapes; if a field is
  insufficient, raise a `contract-change-request` for window C to arbitrate
  (per PLAN constraint and path-ownership rules).

At integration (window C, step "锁定 contracts-v1"), window B's
`packages/execution-domain` models are reconciled to import directly from
`packages/contracts`, and these fixtures are asserted equal to the authoritative
schemas.

| File | Contract | Direction for window B |
|---|---|---|
| `approved_trade_intent_v1.schema.json` | `ApprovedTradeIntentV1` (§3.2) | **inbound** — consumed from control-plane outbox/stream |
| `execution_event_envelope_v1.schema.json` | `ExecutionEventEnvelopeV1` (§3.3) | **outbound** — produced by the Projection Actor to control-plane |
| `data_quality_envelope_v1.schema.json` | data-quality envelope (§2.2) | **inbound** — wraps every dashboard/snapshot response |
| `hermes_decision_v1.schema.json` | `HermesDecisionV1` (§3.1) | read-only — used for end-to-end trace assertions |
