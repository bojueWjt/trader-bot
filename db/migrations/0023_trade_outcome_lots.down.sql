-- Fail closed: never delete unknown lots or drop uniqueness onto duplicates.
DO $$
DECLARE
    null_intents bigint;
    duplicate_pairs bigint;
BEGIN
    SELECT count(*) INTO null_intents
    FROM trade_outcomes
    WHERE intent_id IS NULL;

    IF null_intents > 0 THEN
        RAISE EXCEPTION
            '0023 down refused: % trade_outcomes rows have null intent_id',
            null_intents;
    END IF;

    SELECT count(*) INTO duplicate_pairs
    FROM (
        SELECT intent_id, account_id
        FROM trade_outcomes
        WHERE intent_id IS NOT NULL
        GROUP BY intent_id, account_id
        HAVING count(*) > 1
    ) duplicated;

    IF duplicate_pairs > 0 THEN
        RAISE EXCEPTION
            '0023 down refused: % duplicate (intent_id, account_id) groups',
            duplicate_pairs;
    END IF;
END
$$;

DROP INDEX IF EXISTS idx_trade_outcomes_account_lot_key;

ALTER TABLE trade_outcomes
    DROP CONSTRAINT IF EXISTS ck_trade_outcomes_attribution;

ALTER TABLE trade_outcomes
    DROP CONSTRAINT IF EXISTS uq_trade_outcomes_account_lot;

ALTER TABLE trade_outcomes
    DROP COLUMN IF EXISTS lot_key,
    DROP COLUMN IF EXISTS position_side,
    DROP COLUMN IF EXISTS attribution;

ALTER TABLE trade_outcomes
    ALTER COLUMN intent_id SET NOT NULL;

ALTER TABLE trade_outcomes
    ADD CONSTRAINT uq_trade_outcomes_intent_account UNIQUE (intent_id, account_id);
