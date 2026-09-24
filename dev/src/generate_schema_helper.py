import string
from collections import defaultdict
from string import Formatter
from textwrap import dedent, indent
from typing import Any, cast

from .helper_get_names import HelperGetNames, InternalHelper, TableFieldType
from .typing import PG_TYPES, SchemaZoneTexts, SQL_Delete_Update_Options, SubstDict


class Helper:
    FILE_TEMPLATE_HEADER = dedent("""
        -- schema_relational.sql for initial database setup OpenSlides
        -- Code generated. DO NOT EDIT.
        """)
    FILE_TEMPLATE_CONSTANT_DEFINITIONS = dedent("""
        CREATE EXTENSION hstore;  -- included in standard postgres-installations, check for alpine

        CREATE FUNCTION generate_sequence()
        RETURNS trigger
        AS $sequences_trigger$
        -- Creates a sequence for the id given by depend_field NEW data if it doesn't exist.
        -- Writes the next value to for this sequence to NEW.
        -- In case a number is given in actual_column of the NEW record that is used
        -- and the corresponding sequence increased if necessary.
        -- Usage with 3 parameters IN TRIGGER DEFINITION:
        -- table_name: table this is treated for
        -- actual_column: column that will be filled with the actual value
        -- depend_field: field that differentiates the sequences. usually meeting_id
        DECLARE
            table_name TEXT := TG_ARGV[0];
            actual_column TEXT := TG_ARGV[1];
            depend_field TEXT := TG_ARGV[2];
            depend_field_id INTEGER;
            sequence_name TEXT;
            sequence_value INTEGER;
            sequence_max INTEGER;
        BEGIN
            depend_field_id := hstore(NEW) -> (depend_field);
            sequence_name := table_name || '_' || depend_field || depend_field_id || '_' || actual_column || '_seq';
            EXECUTE format('CREATE SEQUENCE IF NOT EXISTS %I OWNED BY %I.%I', sequence_name, table_name, actual_column);
            sequence_value := hstore(NEW) -> actual_column;
            IF sequence_value IS NULL THEN
                sequence_value := nextval(sequence_name);
            ELSE
                EXECUTE format('SELECT last_value FROM %I', sequence_name) INTO sequence_max;
                -- <= because the unused sequence starts with last_value=1 and is_called=f and needs to be written to.
                IF sequence_max <= sequence_value THEN
                    SELECT setval(sequence_name, sequence_value) INTO sequence_value;
                END IF;
            END IF;
            RETURN populate_record(NEW, format('%s=>%s',actual_column, sequence_value)::hstore);
        END;
        $sequences_trigger$
        LANGUAGE plpgsql;

        CREATE TABLE os_notify_log_t (
            id integer PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
            operation varchar(32),
            fqid varchar(256) NOT NULL,
            updated_fields varchar(63)[],
            xact_id xid8,
            timestamp timestamptz,
            CONSTRAINT unique_fqid_xact_id_operation UNIQUE (operation,fqid,xact_id)
        );

        CREATE TABLE version (
            migration_index INTEGER PRIMARY KEY,
            migration_state TEXT,
            replace_tables JSONB
        );

        -- Log functions

        CREATE OR REPLACE PROCEDURE log_field_change(
            operation_var TEXT,
            fqid_var TEXT,
            fields TEXT[]
        ) AS
        $log_field_change$
        BEGIN
            INSERT INTO os_notify_log_t (operation, fqid, xact_id, timestamp, updated_fields)
            VALUES (operation_var, fqid_var, pg_current_xact_id(), now(), fields)
            ON CONFLICT (operation, fqid, xact_id) DO UPDATE SET updated_fields = (
                SELECT ARRAY(
                    SELECT DISTINCT e
                    FROM unnest(COALESCE(os_notify_log_t.updated_fields, '{}'::varchar[])) AS e
                    UNION
                    SELECT DISTINCT e
                    FROM unnest(COALESCE(EXCLUDED.updated_fields, '{}'::varchar[])) AS e
                )
            );
        END;
        $log_field_change$ LANGUAGE plpgsql;

        CREATE FUNCTION log_modified_models() RETURNS trigger AS $log_modified_trigger$
        DECLARE
            escaped_table_name varchar;
            operation_var TEXT;
            fqid_var TEXT;
            updated_fields_var varchar(63)[];
            old_hstore hstore;
            new_hstore hstore;
        BEGIN
            escaped_table_name := TG_ARGV[0];
            operation_var := LOWER(TG_OP);

            -- Determine fqid (use OLD for deletes)
            fqid_var := escaped_table_name || '/' || NEW.id;
            IF (TG_OP = 'DELETE') THEN
                fqid_var := escaped_table_name || '/' || OLD.id;
            END IF;

            updated_fields_var := NULL;
            IF (TG_OP = 'UPDATE') THEN
                old_hstore := hstore(OLD);
                new_hstore := hstore(NEW);
                updated_fields_var := akeys((new_hstore - old_hstore) || (old_hstore - new_hstore));
            END IF;

            CALL log_field_change(operation_var, fqid_var, updated_fields_var);

            RETURN NULL;  -- returning NULL because AFTER TRIGGER return value is ignored
        END;
        $log_modified_trigger$ LANGUAGE plpgsql;

        CREATE FUNCTION notify_transaction_end() RETURNS trigger AS $notify_trigger$
        DECLARE
            payload TEXT;
            body_content_text TEXT;
        BEGIN
            -- Running the trigger for the first time in a transaction creates the table and after committing the transaction the table is dropped.
            -- Every next run of the trigger in this transaction raises a notice that the table exists. Setting the log_min_messages to notice increases the noise because of such messages.
            CREATE LOCAL TEMPORARY TABLE
            IF NOT EXISTS tbl_notify_counter_tx_once (
                "id" integer NOT NULL PRIMARY KEY GENERATED ALWAYS AS IDENTITY
            ) ON COMMIT DROP;

            -- If running for the first time, the transaction id is send via os_notify.
            IF NOT EXISTS (SELECT * FROM tbl_notify_counter_tx_once) THEN
                INSERT INTO tbl_notify_counter_tx_once DEFAULT VALUES;
                payload := '{"xactId":' ||
                    pg_current_xact_id() ||
                    '}';
                PERFORM pg_notify('os_notify', payload);
            END IF;

            RETURN NULL;  -- returning NULL because AFTER TRIGGER return value is ignored
        END;
        $notify_trigger$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION log_modified_related_models()
        RETURNS trigger AS $log_modified_related_trigger$
        DECLARE
            fqid_var TEXT;
            ref_column TEXT;
            fk_field TEXT;
            foreign_table TEXT;
            foreign_id TEXT;
            i INTEGER := 0;
        BEGIN

            WHILE i < TG_NARGS LOOP
                foreign_table := TG_ARGV[i];
                ref_column := TG_ARGV[i+1];
                fk_field := TG_ARGV[i+2];

                IF (TG_OP = 'DELETE') THEN
                    EXECUTE format('SELECT ($1).%I', ref_column) INTO foreign_id USING OLD;
                ELSE
                    EXECUTE format('SELECT ($1).%I', ref_column) INTO foreign_id USING NEW;
                END IF;

                IF foreign_id IS NOT NULL THEN
                    fqid_var := foreign_table || '/' || foreign_id;
                    CALL log_field_change('update', fqid_var, ARRAY[fk_field]);
                END IF;

                --when update there must be a notification for the old foreign_fqid
                IF (TG_OP = 'UPDATE') THEN
                    EXECUTE format('SELECT ($1).%I', ref_column) INTO foreign_id USING OLD;
                    IF foreign_id IS NOT NULL THEN
                        fqid_var := foreign_table || '/' || foreign_id;
                        CALL log_field_change('update', fqid_var, ARRAY[fk_field]);
                    END IF;
                END IF;

                i := i + 3;
            END LOOP;

            RETURN NULL;  -- returning NULL because AFTER TRIGGER return value is ignored
        END;
        $log_modified_related_trigger$ LANGUAGE plpgsql;
    """)
    FILE_TEMPLATE_CONSTANT_TRIGGERS = dedent("""
        -- Validation triggers

        CREATE OR REPLACE FUNCTION is_timezone( tz TEXT ) RETURNS BOOLEAN as $$
        DECLARE
            is_valid BOOLEAN;
        BEGIN
            IF tz IS NULL THEN
                RETURN TRUE;
            END IF;

            SELECT EXISTS (SELECT 1 FROM pg_timezone_names WHERE name=tz) INTO is_valid;
            RETURN is_valid;
        END;
        $$ language plpgsql STABLE;

        CREATE FUNCTION check_unique_ids_pair()
        RETURNS trigger
        AS $unique_ids_pair_trigger$
        -- usage with 1 parameter IN TRIGGER DEFINITION:
        -- base_column_name: name of write fields before adding numeric suffixes
        -- Guards against mirrored duplicates by skipping one of the pairs.
        DECLARE
            base_column_name text;
            value_1 integer;
            value_2 integer;
        BEGIN
            base_column_name := TG_ARGV[0];
            value_1 := hstore(NEW) -> (base_column_name || '_1');
            value_2 := hstore(NEW) -> (base_column_name || '_2');

            IF (value_1 > value_2) THEN
                RETURN NULL;
            END IF;

            RETURN NEW;
        END;
        $unique_ids_pair_trigger$
        LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION prevent_writes() RETURNS trigger AS $read_only_trigger$
        BEGIN
            RAISE EXCEPTION 'Table % is currently read-only.', TG_TABLE_NAME;
        END;
        $read_only_trigger$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION prevent_updates() RETURNS trigger AS $constant_field_trigger$
        DECLARE
            collection TEXT := TG_ARGV[0];
            constant_column TEXT := TG_ARGV[1];
            old_value TEXT := hstore(OLD) -> constant_column;
            new_value TEXT := hstore(NEW) -> constant_column;
        BEGIN
            IF old_value IS DISTINCT FROM new_value THEN
                RAISE EXCEPTION 'Constant value constraint violated for %/%: % can not be updated.', collection, NEW.id, constant_column;
            END IF;
            RETURN NEW;
        END;
        $constant_field_trigger$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION raise_equality_exception_conditionally(check_column TEXT, ref_column TEXT, own_collection TEXT, own_id INTEGER, own_equal_val TEXT, foreign_collection TEXT, foreign_id INTEGER, foreign_equal_val TEXT)
        RETURNS void AS $equality_exception$
        DECLARE
            own_fqid TEXT;
            foreign_fqid TEXT;
        BEGIN
            IF foreign_id IS NOT NULL AND own_id IS NOT NULL THEN
                IF foreign_equal_val IS DISTINCT FROM own_equal_val THEN
                    foreign_fqid := foreign_collection || '/' || foreign_id;
                    IF check_column = 'meeting_id' THEN
                        RAISE EXCEPTION 'The following models do not belong to meeting %: [''%'']', own_equal_val, foreign_fqid;
                    END IF;
                    foreign_fqid := foreign_fqid  || '/' || check_column;
                    own_fqid := own_collection || '/' || own_id || '/' || check_column;
                    RAISE EXCEPTION 'The relation % requires the following fields to be equal:% %: % % %: %', ref_column, chr(10), own_fqid, own_equal_val, chr(10), foreign_fqid, foreign_equal_val;
                END IF;
            END IF;
        END;
        $equality_exception$ LANGUAGE plpgsql;

        -- expects in this order:
        -- * own table name,
        -- * referenced table name,
        -- * field in own table for which the check was triggered
        -- * field that is supposed to be equal
        -- * if new is the back relations table
        CREATE OR REPLACE FUNCTION check_equals()
        RETURNS trigger AS $check_equals_trigger$
        DECLARE
            ref_column TEXT;
            check_column TEXT;
            foreign_collection TEXT;
            foreign_id INTEGER;
            foreign_equal_val TEXT;
            own_id INTEGER;
            own_equal_val TEXT;
            own_collection TEXT;
            from_back_relation BOOLEAN;
            i INTEGER := 0;
        BEGIN

            WHILE i < TG_NARGS LOOP
                own_collection := TG_ARGV[i];
                foreign_collection := TG_ARGV[i+1];
                ref_column := TG_ARGV[i+2];
                check_column := TG_ARGV[i+3];
                from_back_relation := TG_ARGV[i+4];

                IF from_back_relation IS TRUE THEN
                    EXECUTE format(
                        'SELECT ($1).id, ($1).%I',
                        check_column
                    ) INTO foreign_id, foreign_equal_val USING NEW;
                    EXECUTE format(
                        'SELECT "id", %I
                        FROM %I
                        WHERE %I = %L',
                        check_column,
                        own_collection,
                        ref_column,
                        foreign_id
                    ) INTO own_id, own_equal_val;
                ELSE
                    EXECUTE format(
                        'SELECT ($1).id, ($1).%I, ($1).%I',
                        check_column,
                        ref_column
                    ) INTO own_id, own_equal_val, foreign_id USING NEW;
                    EXECUTE format(
                        'SELECT %I
                        FROM %I
                        WHERE "id" = %L',
                        check_column,
                        foreign_collection,
                        foreign_id
                    ) INTO foreign_equal_val;
                END IF;

                PERFORM raise_equality_exception_conditionally(
                    check_column,
                    ref_column,
                    own_collection,
                    own_id,
                    own_equal_val,
                    foreign_collection,
                    foreign_id,
                    foreign_equal_val
                );

                i := i + 5;
            END LOOP;

            RETURN NULL;  -- returning NULL because AFTER TRIGGER return value is ignored
        END;
        $check_equals_trigger$ LANGUAGE plpgsql;

        -- expects in this order:
        -- * intermediate table name,
        -- * column referencing calling table in intermediate table
        -- * calling table name
        -- * column referencing other table in intermediate table
        -- * other table name
        -- * field that is supposed to be equal
        -- * collection definitions-defined name for the relation on the side for which the check was triggered
        CREATE OR REPLACE FUNCTION check_equals_multi()
        RETURNS trigger AS $check_equals_multi_trigger$
        DECLARE
            ref_column TEXT;
            check_column TEXT;
            foreign_collection_reference TEXT;
            foreign_collection TEXT;
            foreign_id INTEGER;
            foreign_equal_val TEXT;
            intermediate_table TEXT;
            own_id INTEGER;
            own_equal_val TEXT;
            own_collection_reference TEXT;
            own_collection TEXT;
            i INTEGER := 0;
            row record;
        BEGIN

            WHILE i < TG_NARGS LOOP
                intermediate_table := TG_ARGV[i];
                own_collection_reference := TG_ARGV[i+1];
                own_collection := TG_ARGV[i+2];
                foreign_collection_reference := TG_ARGV[i+3];
                foreign_collection := TG_ARGV[i+4];
                check_column := TG_ARGV[i+5];
                ref_column := TG_ARGV[i+6];

                own_id = NEW.id;
                FOR row in EXECUTE format('
                    SELECT a.%I AS a_val, c.id AS c_id, c.%I AS c_val
                    FROM %I a
                        JOIN %I b ON b.%I = a.id
                        JOIN %I c ON b.%I = c.id
                    WHERE a.id = %L',
                    check_column,
                    check_column,
                    own_collection,
                    intermediate_table,
                    own_collection_reference,
                    foreign_collection,
                    foreign_collection_reference,
                    own_id
                ) LOOP
                    own_equal_val := row.a_val;
                    foreign_id := row.c_id;
                    foreign_equal_val := row.c_val;

                    PERFORM raise_equality_exception_conditionally(
                        check_column,
                        ref_column,
                        own_collection,
                        own_id,
                        own_equal_val,
                        foreign_collection,
                        foreign_id,
                        foreign_equal_val
                    );
                END LOOP;

                i := i + 7;
            END LOOP;

            RETURN NULL;  -- returning NULL because AFTER TRIGGER return value is ignored
        END;
        $check_equals_multi_trigger$ LANGUAGE plpgsql;

        -- expects in this order:
        -- * intermediate table name,
        -- * column referencing table1 in intermediate table
        -- * table1 name
        -- * column referencing table2 in intermediate table
        -- * table2 name
        -- * field that is supposed to be equal
        -- * collection definitions-defined name for the relation on the side for which the check was triggered
        CREATE OR REPLACE FUNCTION check_equals_intermediate()
        RETURNS trigger AS $check_equals_intermediate_trigger$
        DECLARE
            ref_column TEXT;
            check_column TEXT;
            foreign_collection_reference TEXT;
            foreign_collection TEXT;
            foreign_id INTEGER;
            foreign_equal_val TEXT;
            own_id INTEGER;
            own_equal_val TEXT;
            own_collection_reference TEXT;
            own_collection TEXT;
            i INTEGER := 0;
        BEGIN

            WHILE i < TG_NARGS LOOP
                own_collection_reference := TG_ARGV[i];
                own_collection := TG_ARGV[i+1];
                foreign_collection_reference := TG_ARGV[i+2];
                foreign_collection := TG_ARGV[i+3];
                check_column := TG_ARGV[i+4];
                ref_column := TG_ARGV[i+5];

                EXECUTE format(
                    'SELECT id, %I
                    FROM %I
                    WHERE id = ($1).%I',
                    check_column,
                    own_collection,
                    own_collection_reference
                ) INTO own_id, own_equal_val USING NEW;
                EXECUTE format(
                    'SELECT id, %I
                    FROM %I
                    WHERE id = ($1).%I',
                    check_column,
                    foreign_collection,
                    foreign_collection_reference
                ) INTO foreign_id, foreign_equal_val USING NEW;

                PERFORM raise_equality_exception_conditionally(
                    check_column,
                    ref_column,
                    own_collection,
                    own_id,
                    own_equal_val,
                    foreign_collection,
                    foreign_id,
                    foreign_equal_val
                );

                i := i + 6;
            END LOOP;

            RETURN NULL;  -- returning NULL because AFTER TRIGGER return value is ignored
        END;
        $check_equals_intermediate_trigger$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION check_equals_meeting_id_for_meeting()
        RETURNS trigger AS $check_equals_meeting_id_for_meeting$
        DECLARE
            table_name TEXT;
            ref_column TEXT;
            id INTEGER;
            meeting_id INTEGER;
            reference_id TEXT;
            i INTEGER := 0;
        BEGIN
            WHILE i < TG_NARGS LOOP
                table_name := TG_ARGV[i];
                ref_column := TG_ARGV[i+1];
                EXECUTE format(
                    'SELECT ($1).id, ($1).meeting_id, ($1).%I',
                    ref_column
                ) INTO id, meeting_id, reference_id USING NEW;

                IF reference_id IS NOT NULL THEN
                    PERFORM raise_equality_exception_conditionally(
                        'meeting_id',
                        ref_column,
                        table_name,
                        id,
                        reference_id,
                        'meeting',
                        meeting_id,
                        meeting_id::TEXT
                    );
                END IF;

                i := i + 2;
            END LOOP;

            RETURN NULL;  -- returning NULL because AFTER TRIGGER return value is ignored
        END;
        $check_equals_meeting_id_for_meeting$ LANGUAGE plpgsql;

        """)
    LOG_CALCULATED_ID_ARRAY_TRIGGER_FUNCTION_TEMPLATE = string.Template(dedent("""
            CREATE OR REPLACE FUNCTION log_${trigger_type}_modified_calculated_id_array_field()
            RETURNS trigger AS $$log_modified_calculated_id_array_field_trigger$$
            -- Expects in this order:
            -- 0. log_collection – Target collection for the log entry
            -- 1. log_collection_id_column – Column used to fetch the 'log_collection' id
            --    (ignored if 'log_collection_id_sql' is provided => may be NULL)
            -- 2. log_collection_id_sql – Custom SQL to fetch the 'log_collection' id
            -- 3. log_field – Field to be logged
            -- 4. ${changed_item_state}_item_column – Column used to fetch the value ${changed_item_state_phrase} 'log_field'
            --    (ignored if '${changed_item_state}_item_sql' is provided => may be NULL)
            -- 5. ${changed_item_state}_item_sql – Custom SQL to fetch the value ${changed_item_state_phrase} 'log_field'
            DECLARE
                log_collection TEXT := TG_ARGV[0];
                log_collection_id_column TEXT := TG_ARGV[1];
                log_collection_id_sql TEXT := TG_ARGV[2];
                log_field TEXT := TG_ARGV[3];
                ${changed_item_state}_item_column TEXT := TG_ARGV[4];
                ${changed_item_state}_item_sql TEXT := TG_ARGV[5];

                ${hstore_type}_hstore hstore := hstore(${hstore});
                log_collection_id INTEGER;
                ${changed_item_state}_item INTEGER;
                ${fetched_log_value_state}_log_field_value INTEGER[];
                fqid_var TEXT;
            BEGIN
                -- No related log_collection instance -> return
                IF (log_collection_id_sql <> '') THEN
                    EXECUTE log_collection_id_sql INTO log_collection_id USING ${hstore};
                ELSE
                    log_collection_id := ${hstore_type}_hstore -> log_collection_id_column;
                END IF;

                IF log_collection_id IS NULL THEN
                    RETURN ${trigger_return_value};
                END IF;

                -- No value in column used for log_field -> return
                ${comment}
                IF (${changed_item_state}_item_sql <> '') THEN
                    EXECUTE ${changed_item_state}_item_sql INTO ${changed_item_state}_item USING ${hstore};
                ELSE
                    ${changed_item_state}_item := ${hstore_type}_hstore -> ${changed_item_state}_item_column;
                END IF;

                IF ${changed_item_state}_item IS NULL THEN
                    RETURN ${trigger_return_value};
                END IF;

                -- Add log entry only if log_field value actually changes
                EXECUTE format('SELECT %I from %I where id = %L', log_field, log_collection, log_collection_id) INTO ${fetched_log_value_state}_log_field_value;
                IF ${fetched_log_value_state}_log_field_value IS NULL OR NOT (${changed_item_state}_item = ANY(${fetched_log_value_state}_log_field_value)) THEN
                    fqid_var := log_collection || '/' || log_collection_id;
                    CALL log_field_change('update', fqid_var, ARRAY[log_field]);
                END IF;

                RETURN ${trigger_return_value};
            END;
            $$log_modified_calculated_id_array_field_trigger$$ LANGUAGE plpgsql;
        """))
    NOT_NULL_TRIGGER_FUNCTION_TEMPLATE = string.Template(dedent("""
            CREATE FUNCTION check_not_null_for_${trigger_type}() RETURNS trigger AS $$not_null_trigger$$
            ${docstring}
            DECLARE
            ${parameters_declaration}
            BEGIN
                IF (TG_OP = 'INSERT') THEN
                    -- in case of INSERT the view is checked on itself so the own id is applicable
                    own_id := NEW.id;
                ELSE
                    own_id := hstore(OLD) -> ${foreign_column};
                    EXECUTE format('SELECT 1 FROM %I WHERE "id" = %L', ${query_relation}, own_id) INTO counted;
                    IF (counted IS NULL) THEN
                        -- if the earlier referenced row was deleted (in the same transaction) we can quit.
                        RETURN NULL;
                    END IF;
                END IF;

                ${select_expression}
                IF (counted is NULL) THEN${own_collection_definition}
                    error_message := format('Trigger %s: NOT NULL CONSTRAINT VIOLATED for %s/%s/%s', TG_NAME, own_collection, own_id, own_column);
                    IF ${ud_operations_filter} THEN${foreign_collection_definition}
                        foreign_id := ${foreign_id};
                        error_message := error_message || format(' from relationship before %s/%s/%s', foreign_collection, foreign_id, foreign_column);
                    END IF;
                    RAISE EXCEPTION '%', error_message;
                END IF;
                RETURN NULL;  -- returning NULL because AFTER TRIGGER return value is ignored
            END;
            $$not_null_trigger$$ language plpgsql;
        """))
    ENUM_DEFINITION_TEMPLATE = string.Template(
        "CREATE TYPE ${name} AS ENUM (${values});\n\n"
    )
    COLLECTION_FROM_TABLE_TEMPLATE = string.Template(
        "${parameter} := SUBSTRING(${table_t} FOR LENGTH(${table_t}) - 2);"
    )
    FIELD_TEMPLATE = string.Template(
        "    ${field_name} ${type}${primary_key}${required}${unique}${check_enum}${check_timezone}${minimum}${maximum}${minLength}${default},\n"
    )
    N_M_RELATIONAL_FIELD_TEMPLATE = string.Template(
        indent(
            dedent("""\
        ${field} integer
            CONSTRAINT ${required_constraint_name} NOT NULL
            CONSTRAINT ${fk_name} REFERENCES ${table} (id)
            ON DELETE CASCADE
            INITIALLY DEFERRED,"""),
            "    ",
        )
    )
    N_M_RELATIONAL_FIELD_TEMPLATE = string.Template(
        indent(
            dedent("""\
        ${field} integer
            CONSTRAINT ${required_constraint_name} NOT NULL
            CONSTRAINT ${fk_name} REFERENCES ${table} (id)
            ON DELETE CASCADE
            INITIALLY DEFERRED,"""),
            "    ",
        )
    )
    INTERMEDIATE_TABLE_N_M_RELATION_TEMPLATE = string.Template(dedent("""
            CREATE TABLE ${table_name} (
            ${field1_definition}
            ${field2_definition}
                CONSTRAINT ${pk_constraint_name} PRIMARY KEY (${list_of_keys})
            );
            CREATE INDEX ${index_1} ON ${table_name} (${field1});
            CREATE INDEX ${index_2} ON ${table_name} (${field2});
        """))
    INTERMEDIATE_TABLE_G_M_RELATION_TEMPLATE = string.Template(dedent("""
            CREATE TABLE ${table_name} (
                ${own_table_name_with_ref_column} integer
                    CONSTRAINT ${required_constraint_name_1} NOT NULL
                    CONSTRAINT ${fk_name} REFERENCES ${own_table_name}(${own_table_ref_column})
                    ON DELETE CASCADE
                    INITIALLY DEFERRED,
                ${own_table_column} varchar(100)
                    CONSTRAINT ${required_constraint_name_2} NOT NULL,
            ${foreign_table_ref_lines}
                CONSTRAINT ${valid_constraint_name} CHECK (split_part(${own_table_column}, '/', 1) IN ${tuple_of_foreign_table_names}),
                CONSTRAINT ${unique_constraint_name} UNIQUE (${own_table_name_with_ref_column}, ${own_table_column})
            );
            CREATE INDEX ${index_1} ON ${table_name} (${own_table_name_with_ref_column});
            CREATE INDEX ${index_2} ON ${table_name} (${own_table_column});
            ${content_field_indices}
        """))
    GM_FOREIGN_TABLE_LINE_TEMPLATE = string.Template(
        indent(
            dedent("""\
            ${gm_content_field} integer
                CONSTRAINT ${constraint_name} GENERATED ALWAYS AS (CASE WHEN split_part(${own_table_column}, '/', 1) = '${foreign_view_name}' THEN cast(split_part(${own_table_column}, '/', 2) AS INTEGER) ELSE null END) STORED
                CONSTRAINT ${fk_name} REFERENCES ${foreign_table_name}(id)
                ON DELETE CASCADE
                INITIALLY DEFERRED,"""),
            "    ",
        )
    )
    GM_INDEX_LINE_TEMPLATE = string.Template(
        "CREATE INDEX ${index} ON ${table_name} (${gm_content_field});"
    )

    RELATION_LIST_AGENDA = dedent("""
        /*   Relation-list infos
        Generated: What will be generated for left field
            FIELD: a usual Database field
            SQL: a sql-expression in a view
            ***: Error
        Field Attributes:Field Attributes opposite side
            1: cardinality 1
            1G: cardinality 1 with generic-relation field
            n: cardinality n
            nG: cardinality n with generic-relation-list field
            t: "to" defined
            r: "reference" defined
            s: sql directive inclusive sql-statement
            R: Required
        Model.Field -> Model.Field
            model.field names
        */

        """)

    @staticmethod
    def get_table_letter(table_name: str, letters: list[str] = []) -> str:
        letter = HelperGetNames.get_table_name(table_name)[0]
        count = -1
        start_letter = letter
        while True:
            if letter in letters:
                count += 1
                if count == 0:
                    start_letter = "".join([part[0] for part in table_name.split("_")])[
                        :2
                    ]
                    letter = start_letter
                else:
                    letter = start_letter + str(count)
            else:
                return letter

    @staticmethod
    def get_table_head(table_name: str) -> str:
        return f"\nCREATE TABLE {HelperGetNames.get_table_name(table_name)} (\n"

    @staticmethod
    def get_table_body_end(code: str) -> str:
        code = code[:-2] + "\n"  # last attribute line without ",", but with "\n"
        code += ");\n\n"
        return code

    @staticmethod
    def get_view_head(table_name: str) -> str:
        return f"\nCREATE VIEW {HelperGetNames.get_view_name(table_name)} AS SELECT *"

    @staticmethod
    def get_view_body_end(table_name: str, code: str) -> str:
        # change the code only if there is
        if code:
            # comma and "\n" for the header
            # last attribute line without ",", but with "\n"
            code = ",\n" + code[:-2] + "\n"
        else:
            code = " "
        code += f"FROM {HelperGetNames.get_table_name(table_name)} {Helper.get_table_letter(table_name)};\n\n"
        return code

    @staticmethod
    def get_notify_trigger(table_name: str) -> str:
        trigger_name = HelperGetNames.get_notify_trigger_name(table_name)
        own_table = HelperGetNames.get_table_name(table_name)
        escaped_table_name = "'" + table_name + "'"
        code = f"CREATE TRIGGER {trigger_name} AFTER INSERT OR UPDATE OR DELETE ON {own_table}\n"
        code += f"FOR EACH ROW EXECUTE FUNCTION log_modified_models({escaped_table_name});\n"
        code += f"CREATE CONSTRAINT TRIGGER notify_transaction_end AFTER INSERT OR UPDATE OR DELETE ON {own_table}\n"
        code += "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION notify_transaction_end();\n"
        return code

    @staticmethod
    def get_alter_table_final_code(code: str) -> str:
        return f"-- Alter table final relation commands\n{code}\n\n"

    @staticmethod
    def get_undecided_all(table_name: str, code: str) -> str:
        return (
            f"/*\n Fields without SQL definition for table {table_name}\n\n{code}\n*/\n"
        )

    @staticmethod
    def get_constraint_with_line_break(constraint_name: str, check: str) -> str:
        """
        Returns contraint with the given name and check.
        Adds line break and indentation.
        """
        return f"\n        CONSTRAINT {constraint_name} {check}"

    @staticmethod
    def get_inline_unique_constraint(table_name: str, fname: str) -> str:
        return Helper.get_constraint_with_line_break(
            HelperGetNames.get_unique_constraint_name(table_name, [fname]),
            "UNIQUE",
        )

    @staticmethod
    def get_inline_required_constraint(table_name: str, fname: str) -> str:
        return Helper.get_constraint_with_line_break(
            HelperGetNames.get_required_constraint_name(table_name, fname),
            "NOT NULL",
        )

    @staticmethod
    def get_inline_default_constraint(table_name: str, fname: str, default: str) -> str:
        return Helper.get_constraint_with_line_break(
            HelperGetNames.get_default_constraint_name(table_name, fname),
            f"DEFAULT {default}",
        )

    @staticmethod
    def get_inline_timezone_constraint(table_name: str, fname: str) -> str:
        return Helper.get_constraint_with_line_break(
            HelperGetNames.get_timezone_constraint_name(table_name, fname),
            f"CHECK (is_timezone({fname}))",
        )

    @staticmethod
    def get_inline_minimum_constraint(table_name: str, fname: str, minimum: int) -> str:
        return Helper.get_constraint_with_line_break(
            HelperGetNames.get_minimum_constraint_name(table_name, fname),
            f"CHECK ({fname} >= {minimum})",
        )

    @staticmethod
    def get_inline_maximum_constraint(table_name: str, fname: str, maximum: int) -> str:
        return Helper.get_constraint_with_line_break(
            HelperGetNames.get_maximum_constraint_name(table_name, fname),
            f"CHECK ({fname} <= {maximum})",
        )

    @staticmethod
    def get_inline_minlength_constraint(
        table_name: str, fname: str, minLength: int
    ) -> str:
        return Helper.get_constraint_with_line_break(
            HelperGetNames.get_minlength_constraint_name(table_name, fname),
            f"CHECK (char_length({fname}) >= {minLength})",
        )

    @staticmethod
    def get_inline_color_constraint(table_name: str, fname: str) -> str:
        return Helper.get_constraint_with_line_break(
            HelperGetNames.get_color_constraint_name(table_name, fname),
            f"CHECK ({fname} is null or {fname} ~* '^#[a-f0-9]{{6}}$')",
        )

    @staticmethod
    def get_inline_generated_always_as_constraint(
        own_table: str, generic_fname: str, own_column: str, foreign_table: str
    ) -> str:
        return Helper.get_constraint_with_line_break(
            HelperGetNames.get_generated_always_as_constraint_name(
                own_table, generic_fname
            ),
            f"GENERATED ALWAYS AS (CASE WHEN split_part({own_column}, '/', 1) = '{foreign_table}' THEN cast(split_part({own_column}, '/', 2) AS INTEGER) ELSE null END) STORED",
        )

    @staticmethod
    def split_unique_together_fields(fields: str) -> list[str]:
        return [field_name.strip() for field_name in fields.split(",")]

    @staticmethod
    def get_varchar_max_length(fdata: dict[str, Any], type_: str) -> int:
        if maxLength := fdata.get("maxLength"):
            return maxLength
        return 256

    @classmethod
    def get_unique_together_constraint_definition(
        cls, table: str, fields: list[str], strict: bool
    ) -> str:
        strict_definition = " NULLS NOT DISTINCT" if strict else ""
        return f"    CONSTRAINT {HelperGetNames.get_unique_constraint_name(table, fields)} UNIQUE{strict_definition} ({', '.join(fields)}),\n"

    @staticmethod
    def get_enum_types_definitions() -> str:
        result = "\n"
        for name, values in InternalHelper.ENUMS.items():
            result += Helper.ENUM_DEFINITION_TEMPLATE.substitute(
                {
                    "name": name,
                    "values": ", ".join([f"'{item}'" for item in values]),
                }
            )
        return result

    @staticmethod
    def get_on_action_mode(action: str, delete: bool) -> str:
        if action:
            if (actionUpper := action.upper()) in SQL_Delete_Update_Options:
                return f" ON {'DELETE' if delete else 'UPDATE'} {SQL_Delete_Update_Options(actionUpper)}"
            else:
                raise Exception(f"{action} is not a valid action mode")
        return ""

    @staticmethod
    def get_foreign_key_notify_trigger(
        table_name: str,
        foreign_table: str,
        ref_column: str,
        updated_field: str,
        fk_columns: list[str] | str,
        initially_deferred: bool = False,
        delete_action: str = "",
        update_action: str = "",
    ) -> str:
        trigger_name = HelperGetNames.get_notify_related_trigger_name(
            table_name, ref_column
        )
        own_table = HelperGetNames.get_table_name(table_name)
        return f"""CREATE TRIGGER {trigger_name} AFTER INSERT OR UPDATE OF {ref_column} OR DELETE ON {own_table}
FOR EACH ROW EXECUTE FUNCTION log_modified_related_models('{foreign_table}', '{ref_column}', '{updated_field}');\n"""

    @staticmethod
    def get_log_calculated_id_array_trigger_data(
        view_name: str,
        log_field: str,
        log_trigger: dict[str, str],
        processed_tables: dict[str, int],
    ) -> tuple[str, str, str, str]:
        on_table = log_trigger["on_table"]
        on_columns = log_trigger.get("on_columns")

        if on_table not in processed_tables:
            processed_tables[on_table] = 1
            unique_index = None
        else:
            processed_tables[on_table] += 1
            unique_index = processed_tables[on_table]

        trigger_name_iu, trigger_name_ud = (
            HelperGetNames.get_log_calculated_id_array_trigger_names(
                view_name, log_field, on_table, bool(on_columns), unique_index
            )
        )

        trigger_columns_iu = f" OR UPDATE OF {on_columns}" if on_columns else ""
        trigger_columns_ud = f" UPDATE OF {on_columns} OR" if on_columns else ""

        return (
            trigger_name_iu,
            trigger_name_ud,
            f"BEFORE INSERT{trigger_columns_iu}",
            f"AFTER{trigger_columns_ud} DELETE",
        )

    @staticmethod
    def get_log_calculated_id_array_trigger_definition(
        view_name: str,
        log_field: str,
        log_triggers: list[dict[str, str]],
    ) -> str:
        TRIGGER_TEMPLATE = string.Template(dedent("""\
            CREATE TRIGGER ${trigger_name} ${trigger_operations} ON ${on_table}
            FOR EACH ROW EXECUTE FUNCTION log_${trigger_type}_modified_calculated_id_array_field('${view_name}', '${log_collection_id_column}', '${log_collection_id_sql}', '${log_field}', '${log_value_column}', '${log_value_sql}');
            """))
        processed_tables: dict[str, int] = {}
        parts: list[str] = []

        subst_base = {
            "view_name": view_name,
            "log_field": log_field,
        }

        for log_trigger in log_triggers:
            (
                trigger_name_iu,
                trigger_name_ud,
                trigger_operations_iu,
                trigger_operations_ud,
            ) = Helper.get_log_calculated_id_array_trigger_data(
                view_name, log_field, log_trigger, processed_tables
            )

            subst_common = {
                **subst_base,
                **{
                    attr: (log_trigger.get(attr) or "")
                    for attr in [
                        "log_collection_id_sql",
                        "log_collection_id_column",
                        "log_value_sql",
                        "log_value_column",
                    ]
                },
                "on_table": log_trigger["on_table"],
            }
            subst_iu = {
                **subst_common,
                "trigger_type": "iu",
                "trigger_name": trigger_name_iu,
                "trigger_operations": trigger_operations_iu,
            }

            subst_ud = {
                **subst_common,
                "trigger_type": "ud",
                "trigger_name": trigger_name_ud,
                "trigger_operations": trigger_operations_ud,
            }

            parts.append(TRIGGER_TEMPLATE.substitute(subst_iu))
            parts.append(TRIGGER_TEMPLATE.substitute(subst_ud))

        return "".join(parts)

    @staticmethod
    def get_nm_table_name_and_fields(
        own_table_field: TableFieldType, foreign_table_field: TableFieldType
    ) -> tuple[str, str, str]:
        nm_table_name = HelperGetNames.get_nm_table_name(
            own_table_field, foreign_table_field
        )
        field1 = HelperGetNames.get_field_in_n_m_relation_list(
            own_table_field, foreign_table_field
        )
        field2 = HelperGetNames.get_field_in_n_m_relation_list(
            foreign_table_field, own_table_field
        )
        if field1 == field2:
            field1 += "_1"
            field2 += "_2"
        return nm_table_name, field1, field2

    @staticmethod
    def get_nm_table_for_n_m_relation_lists(
        own_table_field: TableFieldType, foreign_table_field: TableFieldType
    ) -> tuple[str, str, str, str]:
        nm_table_name, field1, field2 = Helper.get_nm_table_name_and_fields(
            own_table_field, foreign_table_field
        )
        table_name = HelperGetNames.get_table_name(nm_table_name)
        table1 = HelperGetNames.get_table_name(own_table_field.table)
        table2 = HelperGetNames.get_table_name(foreign_table_field.table)
        fk_idx1 = HelperGetNames.get_fk_and_index_name(table_name, field1, table1, "id")
        fk_idx2 = HelperGetNames.get_fk_and_index_name(table_name, field2, table2, "id")
        text = Helper.INTERMEDIATE_TABLE_N_M_RELATION_TEMPLATE.substitute(
            {
                "table_name": table_name,
                "field1_definition": Helper.N_M_RELATIONAL_FIELD_TEMPLATE.substitute(
                    {
                        "field": field1,
                        "required_constraint_name": HelperGetNames.get_required_constraint_name(
                            nm_table_name, field1
                        ),
                        "fk_name": fk_idx1[0],
                        "table": table1,
                    }
                ),
                "field2_definition": Helper.N_M_RELATIONAL_FIELD_TEMPLATE.substitute(
                    {
                        "field": field2,
                        "required_constraint_name": HelperGetNames.get_required_constraint_name(
                            nm_table_name, field2
                        ),
                        "fk_name": fk_idx2[0],
                        "table": table2,
                    }
                ),
                "field1": field1,
                "index_1": fk_idx1[1],
                "field2": field2,
                "index_2": fk_idx2[1],
                "pk_constraint_name": HelperGetNames.get_nm_pk_constraint_name(
                    table_name
                ),
                "list_of_keys": ", ".join([field1, field2]),
            }
        )
        return nm_table_name, text, field1, field2

    @staticmethod
    def get_gm_table_config(
        own_table_field: TableFieldType, foreign_table_fields: list[TableFieldType]
    ) -> tuple[str, str, dict[str, TableFieldType], str, str, list[str], list[str]]:
        gm_table_name = HelperGetNames.get_gm_table_name(own_table_field)
        own_table_name = HelperGetNames.get_table_name(own_table_field.table)
        own_table_column = own_table_field.intermediate_column
        own_table_name_with_ref_column = (
            HelperGetNames.get_own_table_name_with_ref_column(own_table_field)
        )

        foreign_table_ref_lines = []
        indices_lines = []
        intermediate_field_to_foreign_table_field: dict[str, TableFieldType] = {}
        for foreign_table_field in foreign_table_fields:
            foreign_table_name = foreign_table_field.table
            gm_content_field = HelperGetNames.get_gm_content_field(
                own_table_column, foreign_table_name
            )
            intermediate_field_to_foreign_table_field[gm_content_field] = (
                foreign_table_field
            )
            fk_idx = HelperGetNames.get_fk_and_index_name(
                gm_table_name, gm_content_field, foreign_table_name, "id"
            )
            subst_dict = {
                "own_table_column": own_table_column,
                "fk_name": fk_idx[0],
                "foreign_table_name": HelperGetNames.get_table_name(foreign_table_name),
                "foreign_view_name": foreign_table_name,
                "gm_content_field": gm_content_field,
                "constraint_name": HelperGetNames.get_generated_always_as_constraint_name(
                    own_table_field.table, own_table_column
                ),
            }
            foreign_table_ref_lines.append(
                Helper.GM_FOREIGN_TABLE_LINE_TEMPLATE.substitute(subst_dict)
            )
            indices_lines.append(
                Helper.GM_INDEX_LINE_TEMPLATE.substitute(
                    {
                        "index": fk_idx[1],
                        "table_name": gm_table_name,
                        "gm_content_field": gm_content_field,
                    }
                )
            )

        return (
            gm_table_name,
            own_table_name_with_ref_column,
            intermediate_field_to_foreign_table_field,
            own_table_name,
            own_table_column,
            foreign_table_ref_lines,
            indices_lines,
        )

    @staticmethod
    def get_gm_table_for_gm_nm_relation_lists(
        own_table_field: TableFieldType, foreign_table_fields: list[TableFieldType]
    ) -> tuple[str, str, str, dict[str, TableFieldType]]:
        joined_table_names = (
            "('"
            + "', '".join(
                [
                    foreign_table_field.table
                    for foreign_table_field in foreign_table_fields
                ]
            )
            + "')"
        )

        (
            gm_table_name,
            own_table_name_with_ref_column,
            intermediate_field_to_foreign_table_field,
            own_table_name,
            own_table_column,
            foreign_table_ref_lines,
            indices_lines,
        ) = Helper.get_gm_table_config(own_table_field, foreign_table_fields)

        fk_idx = HelperGetNames.get_fk_and_index_name(
            gm_table_name,
            own_table_name_with_ref_column,
            own_table_name,
            own_table_field.ref_column,
        )
        text = Helper.INTERMEDIATE_TABLE_G_M_RELATION_TEMPLATE.substitute(
            {
                "table_name": gm_table_name,
                "own_table_name": own_table_name,
                "own_table_name_with_ref_column": own_table_name_with_ref_column,
                "fk_name": fk_idx[0],
                "index_1": fk_idx[1],
                "index_2": HelperGetNames.get_index_name(
                    gm_table_name, own_table_column
                ),
                "own_table_ref_column": own_table_field.ref_column,
                "own_table_column": own_table_column,
                "tuple_of_foreign_table_names": joined_table_names,
                "foreign_table_ref_lines": "\n".join(foreign_table_ref_lines),
                "required_constraint_name_1": HelperGetNames.get_required_constraint_name(
                    gm_table_name, own_table_name_with_ref_column
                ),
                "required_constraint_name_2": HelperGetNames.get_required_constraint_name(
                    gm_table_name, own_table_column
                ),
                "valid_constraint_name": HelperGetNames.get_generic_valid_constraint_name(
                    own_table_field.table, own_table_column
                ),
                "unique_constraint_name": HelperGetNames.get_generic_unique_constraint_name(
                    own_table_name_with_ref_column, own_table_column
                ),
                "content_field_indices": "\n".join(indices_lines),
            }
        )
        return (
            gm_table_name,
            text,
            own_table_name_with_ref_column,
            intermediate_field_to_foreign_table_field,
        )

    @staticmethod
    def get_trigger_for_intermediate_table(
        own_table_field: TableFieldType, foreign_table_field: TableFieldType
    ) -> str:

        field1 = HelperGetNames.get_field_in_n_m_relation_list(
            own_table_field, foreign_table_field
        )
        field2 = HelperGetNames.get_field_in_n_m_relation_list(
            foreign_table_field, own_table_field
        )
        if field1 == field2:
            field1 += "_1"
            field2 += "_2"
        nm_table_name = HelperGetNames.get_nm_table_name(
            own_table_field, foreign_table_field
        )
        table_name = HelperGetNames.get_table_name(nm_table_name)
        trigger_name = HelperGetNames.get_notify_trigger_name(table_name)

        return f"""
CREATE TRIGGER {trigger_name} AFTER INSERT OR UPDATE OR DELETE ON {nm_table_name}
FOR EACH ROW EXECUTE FUNCTION log_modified_related_models('{own_table_field.table}','{field1}','{own_table_field.column}','{foreign_table_field.table}','{field2}','{foreign_table_field.column}');
CREATE CONSTRAINT TRIGGER notify_transaction_end AFTER INSERT OR UPDATE OR DELETE ON {nm_table_name}
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION notify_transaction_end();
"""

    @staticmethod
    def get_trigger_for_generic_relation(
        table_name: str,
        generic_plain_field_name: str,
        updated_field: str,
        foreign_table: str,
    ) -> str:
        trigger_name = HelperGetNames.get_notify_related_trigger_name(
            foreign_table, generic_plain_field_name
        )
        own_table_name = HelperGetNames.get_table_name(table_name)
        return f"""
CREATE TRIGGER {trigger_name} AFTER INSERT OR UPDATE OF {generic_plain_field_name} OR DELETE ON {own_table_name}
FOR EACH ROW EXECUTE FUNCTION log_modified_related_models('{foreign_table}','{generic_plain_field_name}','{updated_field}');
"""

    @staticmethod
    def get_trigger_for_generic_intermediate_table(
        own_table_field: TableFieldType, foreign_table_fields: list[TableFieldType]
    ) -> str:

        gm_table_name = HelperGetNames.get_gm_table_name(own_table_field)
        trigger_text = ""

        for foreign_table_field in foreign_table_fields:
            gm_content_field = HelperGetNames.get_gm_content_field(
                own_table_field.intermediate_column, foreign_table_field.table
            )
            trigger_name = HelperGetNames.get_notify_gm_related_trigger_name(
                gm_content_field, gm_table_name
            )
            own_table_name_with_ref_column = (
                f"{own_table_field.table}_{own_table_field.ref_column}"
            )
            trigger_text += f"""
CREATE TRIGGER {trigger_name} AFTER INSERT OR UPDATE OF {gm_content_field} OR DELETE ON {gm_table_name}
FOR EACH ROW EXECUTE FUNCTION log_modified_related_models('{own_table_field.table}','{own_table_name_with_ref_column}','{own_table_field.column}','{foreign_table_field.table}','{gm_content_field}','{foreign_table_field.column}');
"""
        trigger_text += f"""CREATE CONSTRAINT TRIGGER notify_transaction_end AFTER INSERT OR UPDATE OR DELETE ON {gm_table_name}
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION notify_transaction_end();
"""
        return trigger_text

    @staticmethod
    def get_equal_field_trigger_config(
        table_field: TableFieldType, fields: list[TableFieldType | str]
    ) -> tuple[str, list[str]]:
        """
        Checks the configuration of the relation and returns:
        - The name of the table that should be used
        - If the field can be updated
        """
        collection = table_field.table
        on_update_fields = []
        for field in fields:
            if isinstance(field, TableFieldType):
                # Assume that these are always primary
                field_def = field.field_def
                field_name = field.column
            elif collection == "meeting" and field == "meeting_id":
                field_def = None
            else:
                field_def = InternalHelper.get_models(collection, field)
                field_name = field
            if field_def and not field_def.get("constant"):
                on_update_fields.append(field_name)
        return HelperGetNames.get_table_name(table_field.table), on_update_fields

    @staticmethod
    def get_event_string(on_update_fields: list[str]) -> str:
        if on_update_fields:
            return f"INSERT OR UPDATE OF {', '.join(on_update_fields)}"
        else:
            return "INSERT"

    @staticmethod
    def get_config_for_trigger_definitions_check_equals(
        own_table_field: TableFieldType,
        foreign_table_field: TableFieldType,
        equal_field: str,
        specified_relation_field: str | None = None,
    ) -> tuple[str, str, str | None, str, list[str], list[str], str, str, str]:
        if specified_relation_field is None:
            own_column = own_table_field.column
            if (
                "reference" in own_table_field.field_def
                and "reference" in foreign_table_field.field_def
                and foreign_table_field.field_def.get("type") in ["relation"]
            ):
                raise Exception(
                    f"Cannot generate equal_fields triggers for {own_table_field.collectionfield} and {foreign_table_field.collectionfield}: Both have reference set."
                )
        else:
            own_column = specified_relation_field

        own_table, own_on_update_fields = Helper.get_equal_field_trigger_config(
            own_table_field, [own_table_field, equal_field]
        )
        own_event_str = Helper.get_event_string(own_on_update_fields)
        foreign_table, foreign_on_update_fields = Helper.get_equal_field_trigger_config(
            foreign_table_field, [equal_field]
        )
        own_trigger_name, foreign_trigger_name = (
            HelperGetNames.get_trigger_names_for_check_equals(
                equal_field,
                own_table,
                own_column,
                foreign_table,
                foreign_table_field.column,
                foreign_table_field.table,
            )
        )
        return (
            own_trigger_name,
            own_table,
            foreign_trigger_name,
            foreign_table,
            own_on_update_fields,
            foreign_on_update_fields,
            own_event_str,
            own_table_field.table,
            own_column,
        )

    @staticmethod
    def get_formatted_default_value(
        table_name: str,
        field_name: str,
        default: str | int | bool | float | list[str],
        type_: str,
    ) -> str:
        if isinstance(default, str) or type_ in ("string", "text", "timezone"):
            return f"'{default}'"
        elif isinstance(default, (int, bool, float)):
            return str(default)
        elif isinstance(default, list):
            return '{"' + '", "'.join(default) + '"}' if default else "'{}'"
        else:
            raise Exception(
                f"{table_name}.{field_name}: seems to be an invalid default value"
            )

    @staticmethod
    def get_type_definition(
        table_name: str, fname: str, type_: str, fdata: dict[str, Any]
    ) -> str:
        enum_type: str | None = None
        if (enum_ := fdata.get("enum")) or (
            enum_ := fdata.get("items", {}).get("enum")
        ):
            if isinstance(enum_, str):
                enum_type = HelperGetNames.get_enum_name(enum_)
            elif isinstance(enum_, list) and all(
                isinstance(item, str) for item in enum_
            ):
                enum_type = HelperGetNames.get_enum_name_for_column(table_name, fname)
                InternalHelper.ENUMS[enum_type] = enum_
            else:
                raise Exception(f"{table_name}.{fname}: is an unsupported enum value")
            if "[]" in fdata.get("type", ""):
                enum_type += "[]"
            return enum_type

        if isinstance((pg_type := PG_TYPES[type_]), string.Template):
            if type_ == "color":
                return pg_type.substitute(
                    {
                        "color_constraint": Helper.get_inline_color_constraint(
                            table_name, fname
                        )
                    }
                )
            return pg_type.substitute(
                {
                    "maxLength": Helper.get_varchar_max_length(fdata, type_),
                    "field_name": fname,
                    "table_name": table_name,
                }
            )
        return pg_type

    @staticmethod
    def get_initials(
        table_name: str, fname: str, type_: str, fdata: dict[str, Any]
    ) -> tuple[SubstDict, SchemaZoneTexts]:
        """
        Helper method to generate common constraints and type definitions for all columns.
        """
        text = cast(SchemaZoneTexts, defaultdict(str))
        flist: list[str] = [
            cast(str, form[1])
            for form in Formatter().parse(Helper.FIELD_TEMPLATE.template)
        ]
        subst: SubstDict = cast(SubstDict, {k: "" for k in flist})
        subst.update(
            {
                "field_name": fname,
                "type": Helper.get_type_definition(table_name, fname, type_, fdata),
            }
        )
        if fdata.get("required"):
            if fname == "id":
                subst["required"] = " NOT NULL"
            else:
                subst["required"] = Helper.get_inline_required_constraint(
                    table_name, fname
                )
        if fdata.get("unique"):
            subst["unique"] = Helper.get_inline_unique_constraint(table_name, fname)
        if (default := fdata.get("default")) is not None:
            default_value = Helper.get_formatted_default_value(
                table_name, fname, default, type_
            )
            subst["default"] = Helper.get_inline_default_constraint(
                table_name, fname, default_value
            )
        if type_ == "timezone":
            subst["check_timezone"] = Helper.get_inline_timezone_constraint(
                table_name, fname
            )
        if (minimum := fdata.get("minimum")) is not None:
            subst["minimum"] = Helper.get_inline_minimum_constraint(
                table_name, fname, minimum
            )
        if (maximum := fdata.get("maximum")) is not None:
            subst["maximum"] = Helper.get_inline_maximum_constraint(
                table_name, fname, maximum
            )
        if minLength := fdata.get("minLength"):
            subst["minLength"] = Helper.get_inline_minlength_constraint(
                table_name, fname, minLength
            )
        if comment := fdata.get("description"):
            text["alter_table"] = Helper.get_post_view_comment(
                HelperGetNames.get_table_name(table_name), fname, comment
            )
        return subst, text

    @staticmethod
    def get_post_view_comment(entity_name: str, fname: str, comment: str) -> str:
        comment = comment.replace("'", '"')
        return f"comment on column {entity_name}.{fname} is '{comment}';\n"

    @staticmethod
    def get_generic_combined_fields(
        table_name: str,
        generic_plain_field_name: str,
        own_column: str,
        foreign_field: TableFieldType,
        is_add:bool=False
    ) -> str:
        foreign_table = foreign_field.table
        foreign_card, error = InternalHelper.get_cardinality(foreign_field)
        if error:
            raise Exception(error)
        if foreign_card.startswith("1"):
            unique = Helper.get_inline_unique_constraint(
                table_name, generic_plain_field_name
            )
        else:
            unique = ""

        generated_always_as = Helper.get_inline_generated_always_as_constraint(
            table_name, generic_plain_field_name, own_column, foreign_table
        )
        add_text = "ADD COLUMN " if is_add else ""

        return f"    {add_text}{generic_plain_field_name} integer{unique}{generated_always_as},\n"

    @staticmethod
    def get_generic_field_constraint(
        collection: str, own_column: str, foreign_tables: list[str], is_add:bool=False
    ) -> str:
        constraint_name = HelperGetNames.get_generic_valid_constraint_name(
            collection, own_column
        )
        return f"""    ADD CONSTRAINT {constraint_name} CHECK (split_part({own_column}, '/', 1) IN ('{"','".join(foreign_tables)}')),\n"""

    @staticmethod
    def prefix_error(method_or_str: str, table_name: str, fname: str) -> str:
        return f"    {table_name}/{fname}: {method_or_str}"
