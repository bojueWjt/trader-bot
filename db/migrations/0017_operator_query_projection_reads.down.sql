-- Privilege cleanup is monotonic. Returning to schema 0016 keeps the
-- operator-query role on its established SELECT-only baseline.
GRANT SELECT ON
    accounts_projection,
    execution_events,
    orders_projection
    TO trader_v3_operator_query;

REVOKE UPDATE ON
    accounts_projection,
    execution_events,
    orders_projection
    FROM trader_v3_operator_query;

DO $$
DECLARE
    relation_name text;
    update_columns text;
BEGIN
    FOREACH relation_name IN ARRAY ARRAY[
        'accounts_projection',
        'execution_events',
        'orders_projection'
    ]
    LOOP
        SELECT string_agg(
            format('%I', attribute.attname),
            ', '
            ORDER BY attribute.attnum
        )
        INTO update_columns
        FROM pg_attribute AS attribute
        WHERE attribute.attrelid = to_regclass(
            format('public.%I', relation_name)
        )
          AND attribute.attnum > 0
          AND NOT attribute.attisdropped;

        IF update_columns IS NOT NULL THEN
            EXECUTE format(
                'REVOKE UPDATE (%s) ON TABLE %I FROM trader_v3_operator_query',
                update_columns,
                relation_name
            );
        END IF;
    END LOOP;
END
$$;
