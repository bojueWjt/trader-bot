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
                'CLOSE_ALL',
                'REFRESH_EVIDENCE'
            )
        );
