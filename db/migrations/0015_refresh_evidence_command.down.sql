DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM operator_commands
        WHERE command_type = 'REFRESH_EVIDENCE'
    ) THEN
        RAISE EXCEPTION
            'refresh evidence command rollback requires zero persisted REFRESH_EVIDENCE rows';
    END IF;
END
$$;

ALTER TABLE operator_commands
    DROP CONSTRAINT IF EXISTS ck_operator_commands_type;

ALTER TABLE operator_commands
    ADD CONSTRAINT ck_operator_commands_type
        CHECK (
            command_type IN (
                'HALT',
                'REDUCE',
                'RESUME',
                'CANCEL_ALL',
                'CLOSE_ALL'
            )
        );
