ALTER TYPE hermes_action_v1
    ADD VALUE IF NOT EXISTS 'cancel_order' BEFORE 'hold';

ALTER TYPE approved_trade_action_v1
    ADD VALUE IF NOT EXISTS 'cancel_order';
