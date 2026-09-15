-- Signal execution is a separate queue purpose; shadow rows never gain authority.
ALTER TABLE signal_dispatch_tasks
    ADD COLUMN processing_purpose text NOT NULL DEFAULT 'shadow'
        CHECK (processing_purpose IN ('shadow', 'signal')),
    ADD COLUMN execution_request jsonb,
    ADD COLUMN execution_context jsonb,
    ADD COLUMN operator_intent_id uuid REFERENCES trade_intents(intent_id);

ALTER TABLE signal_dispatch_tasks DROP CONSTRAINT ck_signal_dispatch_status;
ALTER TABLE signal_dispatch_tasks ADD CONSTRAINT ck_signal_dispatch_status CHECK (
    status IN ('pending', 'leased', 'shadow_dispatched', 'expired', 'failed',
               'skipped', 'dispatched', 'reconciling')
);

ALTER TABLE message_processing_runs DROP CONSTRAINT ck_message_processing_runs_processing_purpose;
ALTER TABLE message_processing_runs ADD CONSTRAINT ck_message_processing_runs_processing_purpose
    CHECK (processing_purpose IN ('legacy', 'shadow', 'signal'));

DROP INDEX uq_message_processing_runs_active_purpose;
CREATE UNIQUE INDEX uq_message_processing_runs_active_purpose
    ON message_processing_runs (raw_message_id, processing_purpose)
    WHERE status IN ('started', 'processing') AND processing_purpose <> 'signal';
CREATE UNIQUE INDEX uq_message_processing_runs_active_signal_account
    ON message_processing_runs (raw_message_id, account_id)
    WHERE status IN ('started', 'processing') AND processing_purpose = 'signal';
ALTER TABLE message_processing_runs ADD CONSTRAINT ck_signal_run_account
    CHECK (processing_purpose <> 'signal' OR (account_id IS NOT NULL AND account_id <> ''));

-- A registered request is the durable retry authority. A new claim may refresh
-- an unfinished analysis context only before any execution request exists.
CREATE FUNCTION protect_signal_execution_request() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.processing_purpose IS DISTINCT FROM OLD.processing_purpose THEN
        RAISE EXCEPTION 'signal task processing_purpose is immutable';
    END IF;
    IF OLD.execution_request IS NOT NULL AND (
        NEW.execution_request IS DISTINCT FROM OLD.execution_request OR
        NEW.execution_context IS DISTINCT FROM OLD.execution_context
    ) THEN
        RAISE EXCEPTION 'registered signal execution request/context is immutable';
    END IF;
    IF OLD.operator_intent_id IS NOT NULL AND
       NEW.operator_intent_id IS DISTINCT FROM OLD.operator_intent_id THEN
        RAISE EXCEPTION 'accepted signal operator_intent_id is immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_signal_execution_request_immutable
    BEFORE UPDATE ON signal_dispatch_tasks
    FOR EACH ROW EXECUTE FUNCTION protect_signal_execution_request();

GRANT UPDATE (operator_intent_id, status, updated_at) ON signal_dispatch_tasks
    TO trader_v3_operator_query;
