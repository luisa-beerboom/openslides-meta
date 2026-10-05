import logging
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from textwrap import dedent, indent
from typing import Any, cast

from sqlfluff import fix

from .alter_schema_helper import AlterSchemaHelper
from .generate_schema_helper import Helper
from .helper_get_names import (
    KEYSEPARATOR,
    FieldSqlErrorType,
    HelperGetNames,
    InternalHelper,
    TableFieldType,
)
from .typing import SchemaZoneTexts, SubstDict, TriggerSqlDict

DESTINATION = (Path(__file__).parent / ".." / "sql" / "schema_relational.sql").resolve()


# Set log level for sqlfluff
for name in logging.root.manager.loggerDict:
    if "sqlfluff" in name:
        logging.getLogger(name).setLevel(logging.WARN)


class GenerateCodeBlocks:
    """Main work is done here by recursing the models and their fields and determine the method to use"""

    table_sql: dict[str, str] = {}
    view_sql: dict[str, str] = {}
    alter_table_final_sql: dict[str, str] = {}
    trigger_sql: TriggerSqlDict = defaultdict(
        lambda: defaultdict(lambda: defaultdict(str))
    )
    # Map of intermediate table name and sql definition of all intermediate tables
    intermediate_sql: dict[str, str] = {}
    # In comparison to intermediate_sql, reinitialized for every collection.
    intermediate_tables: dict[str, str] = {}

    if not InternalHelper.MODELS:
        InternalHelper.read_models_yml()

    @classmethod
    def generate_the_code(
        cls,
    ) -> tuple[
        str,
        str,
        str,
        str,
        str,
        str,
        list[str],
        list[str],
        str,
        str,
        str,
        str,
        str,
        str,
        str,
        str,
        str,
        list[str],
    ]:
        """
        Return values:
          enum_definitions: definitions of the enum types
          pre_code: Type definitions, generated trigger definitions etc., which should all appear before first table definitions
          table_name_code: All table definitions
          view_name_code: All view definitions, after all views, because of view field definition by sql
          alter_table_final_code: Changes on tables defining relations after, which should appear after all table/views definition to be sequence independant
          final_info_code: Detailed info about all relation fields.Types: relation, relation-list, generic-relation and generic-relation-list
          missing_handled_atributes: List of unhandled attributes. handled one's are to be set manually.
          missing_handled_collections_meta_attributes: List of unhandled meta-attributes of the collection
          im_table_code: Code for intermediate tables.
              n:m-relations name schema: f"nm_{smaller-table-name}_{it's-fieldname}_{greater-table_name}" uses one per relation
              g:m-relations name schema: f"gm_{table_field.table}_{table_field.column}" of table with generic-list-field
          create_trigger_partitioned_sequences_code: Definitions of triggers calling generate_sequence
          create_trigger_1_1_relation_not_null_code: Definitions of triggers calling check_not_null_for_1_1_relation
          create_trigger_1_n_relation_not_null_code: Definitions of triggers calling check_not_null_for_1_n
          create_trigger_n_m_relation_not_null_code: Definitions of triggers calling check_not_null_for_n_m
          create_trigger_prevent_updates_code: Definitions of triggers calling prevent_updates check
          create_trigger_unique_ids_pair_code: Definitions of triggers calling check_unique_ids_pair
          create_trigger_equal_fields_code: Definitions of triggers checking equal_fields
          create_trigger_notify_code: Definitions of triggers calling notify_modified_models
          errors: to show
        """
        handled_attributes = {
            "required",
            "maxLength",
            "minLength",
            "default",
            "type",
            "restriction_mode",
            "minimum",
            "maximum",
            "calculated",
            "description",
            "read_only",
            "enum",
            "items",
            "to",
            "reference",
            "sequence_scope",
            # "on_delete", # must have other name then the key-value-store one
            "sql",
            "log_triggers",
            "equal_fields",
            "unique",
            "constant",
        }
        collection_meta_handled_attributes = {
            "unique_together",
        }
        enum_definitions: str = ""
        pre_code: str = ""
        table_name_code: str = ""
        view_name_code: str = ""
        alter_table_final_code: str = ""
        create_trigger_partitioned_sequences_code: str = ""
        create_trigger_1_1_relation_not_null_code: str = ""
        create_trigger_1_n_relation_not_null_code: str = ""
        create_trigger_n_m_relation_not_null_code: str = ""
        create_trigger_prevent_updates_code: str = ""
        create_trigger_unique_ids_pair_code: str = ""
        create_trigger_equal_fields_code: str = ""
        create_trigger_notify_code: str = ""
        final_info_code: str = ""
        missing_handled_attributes = []
        missing_handled_collections_meta_attributes = set()
        im_table_code = ""
        errors: list[str] = []

        for type_ in ["iu", "ud"]:
            pre_code += (
                Helper.LOG_CALCULATED_ID_ARRAY_TRIGGER_FUNCTION_TEMPLATE.substitute(
                    cls.get_log_calculated_id_array_trigger_params(type_)
                )
            )
        pre_code += Helper.FILE_TEMPLATE_CONSTANT_TRIGGERS

        for type_ in ["1_1", "1_n", "n_m"]:
            pre_code += Helper.NOT_NULL_TRIGGER_FUNCTION_TEMPLATE.substitute(
                cls.get_not_null_trigger_params(type_)
            )

        for collection_name, data in InternalHelper.MODELS.items():
            if collection_name in ["_migration_index", "_meta"]:
                continue

            fields = data["fields"]
            schema_zone_texts = cast(SchemaZoneTexts, defaultdict(str))

            for fname, fdata in fields.items():
                for attr in fdata:
                    if (
                        attr not in handled_attributes
                        and attr not in missing_handled_attributes
                    ):
                        missing_handled_attributes.append(attr)
                method_or_str, type_ = cls.get_method(fname, fdata)
                if isinstance(method_or_str, str):
                    error = Helper.prefix_error(method_or_str, collection_name, fname)
                    schema_zone_texts["undecided"] += error
                    errors.append(error)
                else:
                    result, error = method_or_str(collection_name, fname, fdata, type_)
                    for k, v in result.items():
                        schema_zone_texts[k] += v or ""  # type: ignore[literal-required]
                        if k.startswith("create_trigger_"):
                            cls.trigger_sql[collection_name][fname][k] += v  # type: ignore[operator]
                    if error:
                        errors.append(
                            Helper.prefix_error(error, collection_name, fname)
                        )

            for attr, value in data.items():
                match attr:
                    case "fields":
                        continue
                    case "unique_together":
                        schema_zone_texts[
                            "table"
                        ] += cls.get_constraint_unique_together(
                            collection_name, value, False
                        )
                    case "unique_together_strict":
                        schema_zone_texts[
                            "table"
                        ] += cls.get_constraint_unique_together(
                            collection_name, value, True
                        )
                    case _:
                        if attr not in collection_meta_handled_attributes:
                            missing_handled_collections_meta_attributes.add(attr)
                        else:
                            raise Exception(
                                f"Attribute '{attr}' set to be handled but actually unhandled."
                            )

            if code := schema_zone_texts["table"]:
                cls.table_sql[collection_name] = Helper.get_table_head(collection_name)
                cls.table_sql[collection_name] += (
                    Helper.get_table_body_end(code) + "\n\n"
                )
                table_name_code += cls.table_sql[collection_name]
            if code := schema_zone_texts["alter_table"]:
                cls.table_sql[collection_name] += code + "\n"
                table_name_code += code + "\n"
            if code := schema_zone_texts["undecided"]:
                table_name_code += Helper.get_undecided_all(collection_name, code)
            cls.view_sql[collection_name] = Helper.get_view_head(collection_name)
            cls.view_sql[collection_name] += Helper.get_view_body_end(
                collection_name, schema_zone_texts.get("view", "")
            )
            if code := schema_zone_texts["post_view"]:
                cls.view_sql[collection_name] += code
            view_name_code += cls.view_sql[collection_name]
            if code := schema_zone_texts["alter_table_final"]:
                cls.alter_table_final_sql[collection_name] = code + "\n"
                alter_table_final_code += code + "\n"
            if code := schema_zone_texts["create_trigger_partitioned_sequences"]:
                create_trigger_partitioned_sequences_code += code + "\n"
            if code := schema_zone_texts["create_trigger_1_1_relation_not_null"]:
                create_trigger_1_1_relation_not_null_code += code + "\n"
            if code := schema_zone_texts["create_trigger_1_n_relation_not_null"]:
                create_trigger_1_n_relation_not_null_code += code + "\n"
            if code := schema_zone_texts["create_trigger_n_m_relation_not_null"]:
                create_trigger_n_m_relation_not_null_code += code + "\n"
            if code := schema_zone_texts["create_trigger_prevent_updates_code"]:
                create_trigger_prevent_updates_code += code + "\n"
            if code := schema_zone_texts["create_trigger_unique_ids_pair_code"]:
                create_trigger_unique_ids_pair_code += code + "\n"
            if code := schema_zone_texts["create_trigger_equal_fields_code"]:
                create_trigger_equal_fields_code += code + "\n"
            if code := schema_zone_texts["final_info"]:
                final_info_code += code + "\n"
            for im_table_name, im_table_def in cls.intermediate_tables.items():
                cls.intermediate_sql[im_table_name] = im_table_def
                im_table_code += im_table_def
            cls.intermediate_tables = {}

            # schema_zone_texts is filled per model field.
            # If any fields for this collection generated table code, create the main notify trigger on it.
            if schema_zone_texts["table"]:
                create_trigger_notify_code += triggercode
            # Special triggers (e.g. for relation fields) come after
            # TODO: needs to be filled in the get_*_relation_*_type functions
            if code := schema_zone_texts["create_trigger_notify"]:
                create_trigger_notify_code += code + "\n"
        enum_definitions = Helper.get_enum_types_definitions()

        return (
            enum_definitions,
            pre_code,
            table_name_code,
            view_name_code,
            alter_table_final_code,
            final_info_code,
            missing_handled_attributes,
            list(missing_handled_collections_meta_attributes),
            im_table_code,
            create_trigger_partitioned_sequences_code,
            create_trigger_1_1_relation_not_null_code,
            create_trigger_1_n_relation_not_null_code,
            create_trigger_n_m_relation_not_null_code,
            create_trigger_prevent_updates_code,
            create_trigger_unique_ids_pair_code,
            create_trigger_equal_fields_code,
            create_trigger_notify_code,
            errors,
        )

    @staticmethod
    def get_not_null_trigger_params(type_: str) -> dict[str, str]:
        if type_ == "1_1":
            docstring = dedent("""\
            -- Parameters required for all operation types
            --   0. own_collection – name of the view on which the trigger is defined
            --   1. own_column – column in `own_table` referencing
            --      `foreign_table`
            --
            -- Parameter needed for extended error message generation for 'UPDATE' and
            -- 'DELETE' (can be empty on INSERT)
            --   2. foreign_collection – name of collection of the triggered table that
            --      will be used to SELECT
            --   3. foreign_column – column in the foreign table referencing
            --      `own_table`""")
            parameters_declaration = indent(
                dedent("""\
                    -- Parameters from TRIGGER DEFINITION
                    -- Always required
                    own_collection TEXT := TG_ARGV[0];
                    own_column TEXT := TG_ARGV[1];

                    -- Only for TG_OP in ('UPDATE', 'DELETE')
                    foreign_collection TEXT := TG_ARGV[2];
                    foreign_column TEXT := TG_ARGV[3];

                    -- Calculated parameters
                    own_id INTEGER;
                    foreign_id INTEGER;
                    counted INTEGER;
                    error_message TEXT;"""),
                "    ",
            )
            select_expression = "EXECUTE format('SELECT %I FROM %I WHERE id = %L', own_column, own_collection, own_id) INTO counted;"

        elif type_ == "1_n":
            docstring = dedent("""\
            -- Parameters required for all operation types
            --   0. own_table – name of the table on which the trigger is defined
            --   1. own_column – column in `own_table` referencing
            --      `foreign_table`
            --   2. foreign_table – name of the triggered table, that will be used to SELECT
            --   3. foreign_column – column in the foreign table referencing
            --      `own_table`""")
            parameters_declaration = indent(
                dedent("""\
                    -- Parameters from TRIGGER DEFINITION
                    -- Always required
                    own_table TEXT := TG_ARGV[0];
                    own_column TEXT := TG_ARGV[1];
                    foreign_table TEXT := TG_ARGV[2];
                    foreign_column TEXT := TG_ARGV[3];

                    -- Calculated parameters
                    own_collection TEXT;
                    foreign_collection TEXT;
                    own_id INTEGER;
                    foreign_id INTEGER;
                    counted INTEGER;
                    error_message TEXT;"""),
                "    ",
            )
            select_expression = "EXECUTE format('SELECT 1 FROM %I WHERE %I = %L', foreign_table, foreign_column, own_id) INTO counted;"

        else:
            docstring = dedent("""\
            -- Parameters required for both INSERT and DELETE operations
            --   0. intermediate_table_name – name of the n:m table
            --   1. own_table – name of the table on which the trigger is defined
            --   2. own_column – column in `own_table` referencing
            --      `foreign_collection`
            --   3. intermediate_table_own_key – column in the n:m table referencing
            --      `own_table`
            --
            -- Parameters needed for extended error message generation for 'DELETE'
            -- (can be empty on INSERT)
            --   4. intermediate_table_foreign_key – column in the n:m table referencing
            --      the foreign table
            --   5. foreign_collection – name of the collection of the foreign table
            --   6. foreign_column – column in the foreign table referencing
            --      `own_collection`""")
            parameters_declaration = indent(
                dedent("""\
                    -- Parameters from TRIGGER DEFINITION
                    -- Always required
                    intermediate_table_name TEXT := TG_ARGV[0];
                    own_table TEXT := TG_ARGV[1];
                    own_column TEXT := TG_ARGV[2];
                    intermediate_table_own_key TEXT := TG_ARGV[3];

                    -- Only for TG_OP = 'DELETE'
                    intermediate_table_foreign_key TEXT := TG_ARGV[4];
                    foreign_collection TEXT := TG_ARGV[5];
                    foreign_column TEXT := TG_ARGV[6];

                    -- Calculated parameters
                    own_collection TEXT;
                    own_id INTEGER;
                    foreign_id INTEGER;
                    counted INTEGER;
                    error_message TEXT;"""),
                "    ",
            )
            select_expression = "EXECUTE format('SELECT 1 FROM %I WHERE %I = %L', intermediate_table_name, intermediate_table_own_key, own_id) INTO counted;"

        return {
            "trigger_type": type_,
            "docstring": docstring,
            "parameters_declaration": parameters_declaration,
            "foreign_column": (
                "intermediate_table_own_key" if type_ == "n_m" else "foreign_column"
            ),
            "query_relation": "own_collection" if type_ == "1_1" else "own_table",
            "select_expression": select_expression,
            "own_collection_definition": (
                ""
                if type_ == "1_1"
                else f"\n        {Helper.COLLECTION_FROM_TABLE_TEMPLATE.substitute({'parameter': 'own_collection', 'table_t': 'own_table'})}"
            ),
            "ud_operations_filter": (
                "(TG_OP = 'DELETE')"
                if type_ == "n_m"
                else "TG_OP IN ('UPDATE', 'DELETE')"
            ),
            "foreign_collection_definition": (
                f"\n            {Helper.COLLECTION_FROM_TABLE_TEMPLATE.substitute({'parameter': 'foreign_collection', 'table_t': 'foreign_table'})}"
                if type_ == "1_n"
                else ""
            ),
            "foreign_id": (
                "hstore(OLD) -> intermediate_table_foreign_key"
                if type_ == "n_m"
                else "OLD.id"
            ),
        }

    @staticmethod
    def get_log_calculated_id_array_trigger_params(type_: str) -> dict[str, str]:
        if type_ == "iu":
            hstore_type = "new"
            comment = "-- Value deletion on update is processed in after-trigger"
        else:
            hstore_type = "old"
            comment = "-- Value adding on update is processed in before-trigger"
        return {
            "trigger_type": type_,
            "changed_item_state": "added" if type_ == "iu" else "deleted",
            "changed_item_state_phrase": (
                "added to" if type_ == "iu" else "deleted from"
            ),
            "hstore_type": hstore_type,
            "hstore": hstore_type.upper(),
            "fetched_log_value_state": "old" if type_ == "iu" else "new",
            "trigger_return_value": "NEW" if type_ == "iu" else "NULL",
            "instance_state": "new" if type_ == "iu" else "deleted",
            "comment": comment,
        }

    @classmethod
    def get_method(
        cls, fname: str, fdata: dict[str, Any]
    ) -> tuple[str | Callable[..., tuple[SchemaZoneTexts, str]], str]:
        """
        returns
        - string or a callable with return value of type SchemaZoneTexts
        - type as string
        """
        if fdata.get("calculated"):
            return (
                f"type:{fdata.get('type')} is marked as a calculated field and not generated in schema\n",
                "",
            )
        if fname == "id":
            type_ = "primary_key"
        else:
            type_ = fdata.get("type", "")
        if method := TYPE_METHOD_MAP.get(type_):
            return (method.__get__(cls), type_)  # returns the callable classmethod
        else:
            return (f"type:{type_} no method defined\n", type_)

    @classmethod
    def get_schema_simple_types(
        cls, table_name: str, fname: str, fdata: dict[str, Any], type_: str
    ) -> tuple[SchemaZoneTexts, str]:
        text, subst = cls.get_text_for_simple_types(table_name, fname, fdata, type_)
        text["table"] = Helper.FIELD_TEMPLATE.substitute(subst)
        if depend_field := fdata.get("sequence_scope"):
            text[
                "create_trigger_partitioned_sequences"
            ] += cls.get_trigger_generate_partitioned_sequence(
                table_name, fname, depend_field
            )
            text["table"] += Helper.get_unique_together_constraint_definition(
                table_name, [fname, depend_field], False
            )
        return text, ""

    @classmethod
    def get_schema_relation_1_1(
        cls, table_name: str, fname: str, fdata: dict[str, Any], type_: str
    ) -> tuple[SchemaZoneTexts, str]:
        text, subst = cls.get_text_for_simple_types(table_name, fname, fdata, type_)
        subst["unique"] = Helper.get_inline_unique_constraint(table_name, fname)
        text["table"] = Helper.FIELD_TEMPLATE.substitute(subst)
        return text, ""

    @classmethod
    def get_text_for_simple_types(
        cls, table_name: str, fname: str, fdata: dict[str, Any], type_: str
    ) -> tuple[SchemaZoneTexts, SubstDict]:
        text = cast(SchemaZoneTexts, defaultdict(str))
        subst, szt = Helper.get_initials(table_name, fname, type_, fdata)
        text.update(szt)
        if fdata.get("constant"):
            text["create_trigger_prevent_updates_code"] = (
                cls.get_trigger_prevent_updates(table_name, fname)
            )
        return text, subst

    @classmethod
    def get_schema_color(
        cls, table_name: str, fname: str, fdata: dict[str, Any], type_: str
    ) -> tuple[SchemaZoneTexts, str]:
        text = cast(SchemaZoneTexts, defaultdict(str))
        subst, szt = Helper.get_initials(table_name, fname, type_, fdata)
        text.update(szt)
        text["table"] = Helper.FIELD_TEMPLATE.substitute(subst)
        return text, ""

    @classmethod
    def get_schema_primary_key(
        cls, table_name: str, fname: str, fdata: dict[str, Any], type_: str
    ) -> tuple[SchemaZoneTexts, str]:
        text = cast(SchemaZoneTexts, defaultdict(str))
        subst, tmp = Helper.get_initials(table_name, fname, type_, fdata)
        text.update(tmp)
        subst["primary_key"] = " PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY"
        text["table"] = Helper.FIELD_TEMPLATE.substitute(subst)
        return text, ""

    @classmethod
    def get_relation_type(
        cls, table_name: str, fname: str, fdata: dict[str, Any], type_: str
    ) -> tuple[SchemaZoneTexts, str]:
        text = cast(SchemaZoneTexts, defaultdict(str))
        own_table_field = TableFieldType(table_name, fname, fdata)
        foreign_table_field: TableFieldType = (
            TableFieldType.get_definitions_from_foreign(
                fdata.get("to"), fdata.get("reference")
            )
        )
        state, _, final_info, error = InternalHelper.check_relation_definitions(
            own_table_field, [foreign_table_field]
        )

        foreign_table = foreign_table_field.table
        if state == FieldSqlErrorType.FIELD:
            foreign_card, error = InternalHelper.get_cardinality(foreign_table_field)
            if foreign_card.startswith("1"):
                text, error = cls.get_schema_relation_1_1(
                    table_name, fname, fdata, "number"
                )
            else:
                text, error = cls.get_schema_simple_types(
                    table_name, fname, fdata, "number"
                )
            if equal_fields := cls.get_equal_fields(
                own_table_field, foreign_table_field
            ):
                text["create_trigger_equal_fields_code"] = (
                    cls.get_trigger_definitions_check_equals(
                        equal_fields, own_table_field, foreign_table_field, state
                    )
                )
            text["alter_table_final"] = (
                AlterSchemaHelper.get_foreign_key_table_constraint_as_alter_table(
                    table_name,
                    foreign_table,
                    fname,
                    foreign_table_field.ref_column,
                )
            )
            table_name = HelperGetNames.get_table_name(table_name)
            text["create_trigger_notify"] = Helper.get_foreign_key_notify_trigger(
                table_name,
                foreign_table_field.table,
                fname,
                foreign_table_field.column,
            )
        elif state == FieldSqlErrorType.SQL:
            if sql := fix(fdata.get("sql", "")):
                text["view"] = sql + ",\n"
            else:
                if foreign_table_field.field_def["type"] == "generic-relation":
                    foreign_column = f"{foreign_table_field.column}_{own_table_field.table}_{own_table_field.ref_column}"
                else:
                    foreign_column = foreign_table_field.column
                text["view"] = cls.get_sql_for_relation_1_1(
                    table_name,
                    fname,
                    foreign_table_field.ref_column,
                    foreign_table,
                    foreign_column,
                )
                if own_table_field.field_def.get("required"):
                    text["create_trigger_1_1_relation_not_null"] = (
                        cls.get_trigger_check_not_null_for_1_1_relation(
                            own_table_field.table,
                            own_table_field.column,
                            foreign_table_field.table,
                            foreign_column,
                        )
                    )
        text["final_info"] = final_info
        return text, error

    @classmethod
    def get_sql_for_relation_1_1(
        cls,
        table_name: str,
        fname: str,
        ref_column: str,
        foreign_table: str,
        foreign_column: str,
    ) -> str:
        table_letter = Helper.get_table_letter(table_name)
        letters = [table_letter]
        foreign_letter = Helper.get_table_letter(foreign_table, letters)
        foreign_table = HelperGetNames.get_table_name(foreign_table)
        return f"(select {foreign_letter}.{ref_column} from {foreign_table} {foreign_letter} where {foreign_letter}.{foreign_column} = {table_letter}.{ref_column}) as {fname},\n"

    @classmethod
    def get_relation_list_type(
        cls, table_name: str, fname: str, fdata: dict[str, Any], type_: str
    ) -> tuple[SchemaZoneTexts, str]:
        text = cast(SchemaZoneTexts, defaultdict(str))
        own_table_field = TableFieldType(table_name, fname, fdata)
        foreign_table_field: TableFieldType = (
            TableFieldType.get_definitions_from_foreign(
                fdata.get("to"),
                fdata.get("reference"),
            )
        )
        state, primary, final_info, error = InternalHelper.check_relation_definitions(
            own_table_field, [foreign_table_field]
        )

        if state != FieldSqlErrorType.ERROR:
            if primary:
                if foreign_table_field.field_def.get("type") == "relation-list":
                    (
                        nm_table_name,
                        definition_text,
                        own_intermediate_field,
                        foreign_intermediate_field,
                    ) = Helper.get_nm_table_for_n_m_relation_lists(
                        own_table_field, foreign_table_field
                    )
                    if equal_fields := cls.get_equal_fields(
                        own_table_field, foreign_table_field
                    ):
                        text["create_trigger_equal_fields_code"] = (
                            cls.get_trigger_definitions_check_equals_multi(
                                equal_fields,
                                own_table_field,
                                foreign_table_field,
                                nm_table_name,
                                own_intermediate_field,
                                foreign_intermediate_field,
                                is_generic_list=False,
                            )
                        )
                    if nm_table_name not in cls.intermediate_tables:
                        cls.intermediate_tables[nm_table_name] = definition_text
                        text["create_trigger_notify"] = (
                            Helper.get_trigger_for_intermediate_table(
                                own_table_field,
                                foreign_table_field,
                            )
                        )
                    else:
                        raise Exception(
                            f"Tried to create im_table '{nm_table_name}' twice"
                        )
            if sql := fdata.get("sql", ""):
                text["view"] = sql + ",\n"
                text["create_trigger_notify"] = (
                    "\n"
                    + (
                        Helper.get_log_calculated_id_array_trigger_definition(
                            table_name,
                            fname,
                            fdata.get("log_triggers", {}),
                        )
                    )
                    + "\n"
                )
            else:
                foreign_table_column = cast(str, foreign_table_field.column)
                foreign_table_field_ref_id = cast(str, foreign_table_field.ref_column)
                if foreign_table_column or foreign_table_field_ref_id:
                    if (
                        type_ := foreign_table_field.field_def.get("type", "")
                    ) == "generic-relation":
                        own_ref_column = own_table_field.ref_column
                        foreign_table_column += (
                            f"_{table_name}_{foreign_table_field.ref_column}"
                        )
                        foreign_table_name = HelperGetNames.get_table_name(
                            foreign_table_field.table
                        )
                        foreign_table_ref_column = foreign_table_field.ref_column
                    elif type_ == "relation-list":
                        if own_table_field.table == foreign_table_field.table:
                            """Example: committee.forward_to_committee_ids to committee.receive_forwardings_from_committee_ids"""
                            own_ref_column = own_table_field.ref_column
                            foreign_table_ref_column = fname[:-1]
                            foreign_table_name = HelperGetNames.get_nm_table_name(
                                own_table_field, foreign_table_field
                            )
                            foreign_table_column = (
                                foreign_table_field.intermediate_column
                            )
                        else:
                            own_ref_column = own_table_field.ref_column
                            foreign_table_ref_column = f"{foreign_table_field.table}_{foreign_table_field.ref_column}"
                            foreign_table_name = HelperGetNames.get_nm_table_name(
                                own_table_field, foreign_table_field
                            )
                            foreign_table_column = (
                                f"{own_table_field.table}_{own_table_field.ref_column}"
                            )
                    elif type_ == "generic-relation-list":
                        own_ref_column = own_table_field.ref_column
                        foreign_table_ref_column = f"{foreign_table_field.table}_{foreign_table_field.ref_column}"
                        foreign_table_name = HelperGetNames.get_gm_table_name(
                            foreign_table_field
                        )
                        foreign_table_column = (
                            f"{foreign_table_field.intermediate_column}_{table_name}_id"
                        )
                    elif type_ == "relation" or foreign_table_field_ref_id:
                        own_ref_column = own_table_field.ref_column
                        foreign_table_ref_column = foreign_table_field.ref_column
                        foreign_table_name = HelperGetNames.get_table_name(
                            foreign_table_field.table
                        )
                        foreign_table_column = foreign_table_field.column
                    else:
                        raise Exception(
                            f"Still not implemented for foreign_table type '{type_}' in False case"
                        )
                text["view"] = cls.get_sql_for_relation_n_1(
                    table_name,
                    fname,
                    own_ref_column,
                    foreign_table_name,
                    foreign_table_column,
                    foreign_table_ref_column,
                    own_table_field.field_def == foreign_table_field.field_def,
                )
                if own_table_field.field_def.get("required"):
                    if (
                        type_ := foreign_table_field.field_def.get("type", "")
                    ) == "relation":
                        text["create_trigger_1_n_relation_not_null"] = (
                            cls.get_trigger_check_not_null_for_1_n(
                                own_table_field.table,
                                own_table_field.column,
                                foreign_table_field.table,
                                foreign_table_field.column,
                            )
                        )
                    elif type_ == "relation-list":
                        text["create_trigger_n_m_relation_not_null"] = (
                            cls.get_trigger_check_not_null_for_n_m(
                                own_table_field, foreign_table_field
                            )
                        )
                if (
                    own_table_field.table == foreign_table_field.table
                    and own_table_field.column == foreign_table_field.column
                ):
                    text["create_trigger_unique_ids_pair_code"] = (
                        cls.get_trigger_check_unique_ids_pair(
                            own_table_field.table,
                            own_table_field.column,
                            HelperGetNames.get_nm_table_name(
                                own_table_field, foreign_table_field
                            ),
                        )
                    )
        if comment := fdata.get("description"):
            text["post_view"] += Helper.get_post_view_comment(
                HelperGetNames.get_view_name(table_name), fname, comment
            )
        text["final_info"] = final_info
        return text, error

    @classmethod
    def get_sql_for_relation_n_1(
        cls,
        table_name: str,
        fname: str,
        own_ref_column: str,
        foreign_table_name: str,
        foreign_table_column: str,
        foreign_table_ref_column: str,
        self_reference: bool = False,
    ) -> str:
        table_letter = Helper.get_table_letter(table_name)
        foreign_letter = Helper.get_table_letter(foreign_table_name, [table_letter])
        AGG_TEMPLATE = f"select array_agg({foreign_letter}.{{}} ORDER BY {foreign_letter}.{{}}) from {foreign_table_name} {foreign_letter}"
        COND_TEMPLATE = (
            f" where {foreign_letter}.{{}} = {table_letter}.{own_ref_column}"
        )
        if not foreign_table_column or not self_reference:
            query = AGG_TEMPLATE.format(
                foreign_table_ref_column, foreign_table_ref_column
            )
            if foreign_table_column:
                query += COND_TEMPLATE.format(foreign_table_column)
        else:
            assert foreign_table_ref_column == (
                col := foreign_table_column
            ), f"own {col} and foreign {foreign_table_ref_column} should be equal"
            arr1 = AGG_TEMPLATE.format(f"{col}_1", f"{col}_1") + COND_TEMPLATE.format(
                f"{col}_2"
            )
            arr2 = AGG_TEMPLATE.format(f"{col}_2", f"{col}_2") + COND_TEMPLATE.format(
                f"{col}_1"
            )
            query = f"select array_cat(({arr1}), ({arr2}))"
        return f"({query}) as {fname},\n"

    @staticmethod
    def get_constraint_unique_together(
        table_name: str, value: Any, strict: bool
    ) -> str:
        assert isinstance(
            value, list
        ), f"'{table_name}.yml/unique_together' must be a list of field names"
        result = ""
        for fields in value:
            fields = Helper.split_unique_together_fields(fields)
            result += Helper.get_unique_together_constraint_definition(
                table_name, fields, strict
            )
        return result

    @classmethod
    def get_trigger_generate_partitioned_sequence(
        cls, view_name: str, actual_field: str, depend_field: str
    ) -> str:
        table_name = HelperGetNames.get_table_name(view_name)
        trigger_name = HelperGetNames.get_partitioned_sequence_trigger_name(
            view_name, actual_field
        )
        return dedent(f"""
            -- definition trigger generate partitioned sequence number for {table_name}.{actual_field} partitioned by {depend_field}
            CREATE TRIGGER {trigger_name} BEFORE INSERT ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION generate_sequence('{table_name}', '{actual_field}', '{depend_field}');
            """)

    @classmethod
    def get_trigger_check_not_null_for_1_1_relation(
        cls,
        own_collection: str,
        own_column: str,
        foreign_collection: str,
        foreign_column: str,
    ) -> str:
        own_table_t = HelperGetNames.get_table_name(own_collection)
        foreign_table_t = HelperGetNames.get_table_name(foreign_collection)
        return dedent(f"""
            -- definition trigger not null for {own_collection}.{own_column} against {foreign_collection}.{foreign_column}
            CREATE CONSTRAINT TRIGGER {HelperGetNames.get_not_null_1_1_rel_insert_trigger_name(own_collection, own_column)} AFTER INSERT ON {own_table_t} INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION check_not_null_for_1_1('{own_collection}', '{own_column}');

            CREATE CONSTRAINT TRIGGER {HelperGetNames.get_not_null_1_1_rel_upd_del_trigger_name(own_collection, own_column)} AFTER UPDATE OF {foreign_column} OR DELETE ON {foreign_table_t} INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION check_not_null_for_1_1('{own_collection}', '{own_column}', '{foreign_collection}', '{foreign_column}');
            """)

    @classmethod
    def get_trigger_check_not_null_for_1_n(
        cls,
        own_collection: str,
        own_column: str,
        foreign_collection: str,
        foreign_column: str,
    ) -> str:
        own_table_t = HelperGetNames.get_table_name(own_collection)
        foreign_table_t = HelperGetNames.get_table_name(foreign_collection)
        return dedent(f"""
            -- definition trigger not null for {own_collection}.{own_column} against {foreign_collection}.{foreign_column}
            CREATE CONSTRAINT TRIGGER {HelperGetNames.get_not_null_rel_list_insert_trigger_name(own_collection, own_column)} AFTER INSERT ON {own_table_t} INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION check_not_null_for_1_n('{own_table_t}', '{own_column}', '{foreign_table_t}', '{foreign_column}');

            CREATE CONSTRAINT TRIGGER {HelperGetNames.get_not_null_rel_list_upd_del_trigger_name(own_collection, own_column)} AFTER UPDATE OF {foreign_column} OR DELETE ON {foreign_table_t} INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION check_not_null_for_1_n('{own_table_t}', '{own_column}', '{foreign_table_t}', '{foreign_column}');

            """)

    @classmethod
    def get_trigger_check_not_null_for_n_m(
        cls, own_table_field: TableFieldType, foreign_table_field: TableFieldType
    ) -> str:
        own_collection = own_table_field.table
        own_column = own_table_field.column
        own_table = HelperGetNames.get_table_name(own_collection)
        foreign_collection = foreign_table_field.table
        foreign_column = foreign_table_field.column
        intermediate_table_name = HelperGetNames.get_nm_table_name(
            own_table_field, foreign_table_field
        )
        intermediate_table_own_key = HelperGetNames.get_field_in_n_m_relation_list(
            own_table_field, foreign_table_field
        )
        intermediate_table_foreign_key = HelperGetNames.get_field_in_n_m_relation_list(
            foreign_table_field, own_table_field
        )
        trigger_name_insert = HelperGetNames.get_not_null_rel_list_insert_trigger_name(
            own_collection, own_column
        )
        trigger_name_delete = HelperGetNames.get_not_null_rel_list_delete_trigger_name(
            own_collection, own_column
        )
        return dedent(f"""
            -- definition trigger not null for {own_collection}.{own_column} against {foreign_collection}.{foreign_column} through {intermediate_table_name}
            CREATE CONSTRAINT TRIGGER {trigger_name_insert} AFTER INSERT ON {own_table} INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION check_not_null_for_n_m('{intermediate_table_name}', '{own_table}', '{own_column}', '{intermediate_table_own_key}');

            CREATE CONSTRAINT TRIGGER {trigger_name_delete} AFTER DELETE ON {intermediate_table_name} INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION check_not_null_for_n_m('{intermediate_table_name}', '{own_table}', '{own_column}', '{intermediate_table_own_key}', '{intermediate_table_foreign_key}', '{foreign_collection}', '{foreign_column}');

            """)

    @classmethod
    def get_trigger_check_unique_ids_pair(
        cls,
        view: str,
        column: str,
        table_name: str,
    ) -> str:
        base_column_name = column[:-1]
        trigger_name = HelperGetNames.get_unique_ids_trigger_name(view, column)
        return dedent(f"""
            -- definition trigger unique ids pair for {view}.{column}
            CREATE TRIGGER {trigger_name} BEFORE INSERT OR UPDATE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION check_unique_ids_pair('{base_column_name}');

            """)

    @staticmethod
    def get_trigger_prevent_updates(collection_name: str, fname: str) -> str:
        trigger_name = HelperGetNames.get_constant_field_trigger_name(
            collection_name, fname
        )
        table_name = HelperGetNames.get_table_name(collection_name)
        return dedent(f"""
            -- definition trigger prevent_updates for {collection_name}.{fname}
            CREATE TRIGGER {trigger_name} BEFORE UPDATE OF {fname} ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION prevent_updates('{collection_name}', '{fname}');
            """)

    @classmethod
    def get_equal_fields(
        cls,
        *table_fields: TableFieldType,
    ) -> list[str]:
        result: set[str] = set()
        for table_field in table_fields:
            equal_fields = table_field.field_def.get("equal_fields")
            if isinstance(equal_fields, list):
                result.update(equal_fields)
            elif isinstance(equal_fields, str):
                result.add(equal_fields)
            elif equal_fields:
                raise Exception(
                    f"Invalid equal_fields for {table_field.column}: Unknown setting."
                )
        return sorted(result)

    @classmethod
    def equal_fields_state_check(
        cls, state: FieldSqlErrorType, table_field: TableFieldType
    ) -> None:
        if state != FieldSqlErrorType.FIELD:
            raise Exception(
                f"Could not write equal_fields trigger for {table_field.column}: Not supported for FieldSqlErrorType {state}."
            )

    @classmethod
    def get_trigger_definitions_check_equals(
        cls,
        equal_fields: list[str],
        own_table_field: TableFieldType,
        foreign_table_field: TableFieldType,
        state: FieldSqlErrorType,
        specified_relation_field: str | None = None,
    ) -> str:
        cls.equal_fields_state_check(state, own_table_field)
        sql = ""
        for equal_field in equal_fields:
            (
                own_trigger_name,
                own_table,
                foreign_trigger_name,
                foreign_table,
                own_on_update_fields,
                foreign_on_update_fields,
                own_event_str,
                own_collection,
                own_column,
            ) = Helper.get_config_for_trigger_definitions_check_equals(
                own_table_field,
                foreign_table_field,
                equal_field,
                specified_relation_field,
            )
            if not foreign_trigger_name:
                sql += dedent(f"""
                    CREATE CONSTRAINT TRIGGER {own_trigger_name} AFTER {own_event_str} ON {own_table} INITIALLY DEFERRED
                    FOR EACH ROW EXECUTE FUNCTION check_equals_meeting_id_for_meeting('{own_table_field.table}', '{own_column}');

                """)
            else:
                foreign_event_str = Helper.get_event_string(foreign_on_update_fields)
                sql += dedent(f"""
                    CREATE CONSTRAINT TRIGGER {own_trigger_name} AFTER {own_event_str} ON {own_table} INITIALLY DEFERRED
                    FOR EACH ROW EXECUTE FUNCTION check_equals('{own_table_field.table}', '{foreign_table_field.table}', '{own_column}', '{equal_field}', FALSE);
                    CREATE CONSTRAINT TRIGGER {foreign_trigger_name} AFTER {foreign_event_str} ON {foreign_table} INITIALLY DEFERRED
                    FOR EACH ROW EXECUTE FUNCTION check_equals('{own_table_field.table}', '{foreign_table_field.table}', '{own_column}', '{equal_field}', TRUE);

                """)
        return sql

    @classmethod
    def get_trigger_definitions_check_equals_multi(
        cls,
        equal_fields: list[str],
        own_table_field: TableFieldType,
        foreign_table_field: TableFieldType,
        nm_table_name: str,
        own_intermediate_field: str,
        foreign_intermediate_field: str,
        is_generic_list: bool,
    ) -> str:
        sql = ""
        for equal_field in equal_fields:
            own_table, own_on_update_fields = Helper.get_equal_field_trigger_config(
                own_table_field, [equal_field]
            )
            own_event_str = Helper.get_event_string(own_on_update_fields)
            foreign_table, foreign_on_update_fields = (
                Helper.get_equal_field_trigger_config(
                    foreign_table_field, [equal_field]
                )
            )
            foreign_event_str = Helper.get_event_string(foreign_on_update_fields)
            intermediate_event_str = Helper.get_event_string([])
            own_trigger_name, foreign_trigger_name, intermediate_trigger_name = (
                HelperGetNames.get_trigger_names_for_check_equals_multi(
                    equal_field,
                    own_table,
                    own_table_field.column,
                    foreign_table,
                    foreign_table_field.column,
                    is_generic_list,
                )
            )
            sql += dedent(f"""
                CREATE CONSTRAINT TRIGGER {own_trigger_name} AFTER {own_event_str} ON {own_table} INITIALLY DEFERRED
                FOR EACH ROW EXECUTE FUNCTION check_equals_multi('{nm_table_name}', '{own_intermediate_field}', '{own_table_field.table}', '{foreign_intermediate_field}', '{foreign_table_field.table}', '{equal_field}', '{own_table_field.column}');
                CREATE CONSTRAINT TRIGGER {foreign_trigger_name} AFTER {foreign_event_str} ON {foreign_table} INITIALLY DEFERRED
                FOR EACH ROW EXECUTE FUNCTION check_equals_multi('{nm_table_name}', '{foreign_intermediate_field}', '{foreign_table_field.table}', '{own_intermediate_field}', '{own_table_field.table}', '{equal_field}', '{foreign_table_field.column}');
                CREATE CONSTRAINT TRIGGER {intermediate_trigger_name} AFTER {intermediate_event_str} ON {nm_table_name} INITIALLY DEFERRED
                FOR EACH ROW EXECUTE FUNCTION check_equals_intermediate('{own_intermediate_field}', '{own_table_field.table}', '{foreign_intermediate_field}', '{foreign_table_field.table}', '{equal_field}', '{own_table_field.column}');

            """)
        return sql

    @classmethod
    def get_generic_relation_type(
        cls,
        table_name: str,
        fname: str,
        fdata: dict[str, Any],
        type_: str,
        is_add: bool = False,
    ) -> tuple[SchemaZoneTexts, str]:
        text = cast(SchemaZoneTexts, defaultdict(str))
        own_table_field = TableFieldType(table_name, fname, fdata)
        foreign_table_fields: list[TableFieldType] = (
            InternalHelper.get_definitions_from_foreign_list(
                fdata.get("to"), fdata.get("reference")
            )
        )

        state, _, final_info, error = InternalHelper.check_relation_definitions(
            own_table_field, foreign_table_fields
        )

        if state == FieldSqlErrorType.FIELD:
            text, error = cls.get_schema_simple_types(
                table_name, fname, fdata, fdata["type"]
            )
            foreign_tables: list[str] = []
            for foreign_table_field in foreign_table_fields:
                generic_plain_field_name = HelperGetNames.get_generic_plain_field_name(
                    own_table_field.column,
                    foreign_table_field.table,
                    foreign_table_field.ref_column,
                )
                foreign_tables.append(foreign_table_field.table)
                text["table"] += Helper.get_generic_combined_fields(
                    table_name,
                    generic_plain_field_name,
                    own_table_field.column,
                    foreign_table_field,
                    is_add,
                )
                if equal_fields := cls.get_equal_fields(
                    own_table_field, foreign_table_field
                ):
                    text[
                        "create_trigger_equal_fields_code"
                    ] += cls.get_trigger_definitions_check_equals(
                        equal_fields,
                        own_table_field,
                        foreign_table_field,
                        state,
                        generic_plain_field_name,
                    )
                text[
                    "create_trigger_notify"
                ] += Helper.get_log_trigger_for_generic_relation(
                    table_name,
                    generic_plain_field_name,
                    foreign_table_field.column,
                    foreign_table_field.table,
                )
                text[
                    "alter_table_final"
                ] += AlterSchemaHelper.get_foreign_key_table_constraint_as_alter_table(
                    own_table_field.table,
                    foreign_table_field.table,
                    generic_plain_field_name,
                    foreign_table_field.ref_column,
                )
            text["table"] += Helper.get_generic_field_constraint(
                own_table_field.table, own_table_field.column, foreign_tables, is_add
            )
        text["final_info"] = final_info
        return text, error

    @classmethod
    def get_generic_relation_list_type(
        cls, table_name: str, fname: str, fdata: dict[str, Any], type_: str
    ) -> tuple[SchemaZoneTexts, str]:
        text = cast(SchemaZoneTexts, defaultdict(str))
        own_table_field = TableFieldType(table_name, fname, fdata)
        foreign_table_fields: list[TableFieldType] = (
            InternalHelper.get_definitions_from_foreign_list(
                fdata.get("to"), fdata.get("reference")
            )
        )
        state, primary, final_info, error = InternalHelper.check_relation_definitions(
            own_table_field, foreign_table_fields
        )

        if state == FieldSqlErrorType.SQL and primary:
            # create gm-intermediate table
            if primary:
                (
                    gm_foreign_table,
                    value,
                    own_intermediate_field,
                    foreign_intermediate_field_foreign_table_field,
                ) = Helper.get_gm_table_for_gm_nm_relation_lists(
                    own_table_field, foreign_table_fields
                )
                text[
                    "create_trigger_notify"
                ] += Helper.get_trigger_for_generic_intermediate_table(
                    own_table_field, foreign_table_fields
                )
                if gm_foreign_table not in cls.intermediate_tables:
                    cls.intermediate_tables[gm_foreign_table] = value
                else:
                    raise Exception(
                        f"Tried to create gm_table '{gm_foreign_table}' twice"
                    )
                equal_fields_text = ""
                for (
                    foreign_intermediate_field,
                    foreign_table_field,
                ) in foreign_intermediate_field_foreign_table_field.items():
                    if equal_fields := cls.get_equal_fields(
                        own_table_field, foreign_table_field
                    ):
                        equal_fields_text += (
                            cls.get_trigger_definitions_check_equals_multi(
                                equal_fields,
                                own_table_field,
                                foreign_table_field,
                                gm_foreign_table,
                                own_intermediate_field,
                                foreign_intermediate_field,
                                is_generic_list=True,
                            )
                        )
                if equal_fields_text:
                    text["create_trigger_equal_fields_code"] = equal_fields_text

            # add field to view definition of table_name
            text["view"] = cls.get_sql_for_relation_n_1(
                table_name,
                fname,
                own_table_field.ref_column,
                gm_foreign_table,
                f"{own_table_field.table}_{own_table_field.ref_column}",
                own_table_field.intermediate_column,
            )

        text["final_info"] = final_info
        return text, error


class ModelsHelper:
    @staticmethod
    def get_foreign_table_from_to_or_reference(
        to: str | None, reference: str | None
    ) -> str:
        if reference:
            result = InternalHelper.ref_compiled.search(reference)
            if result is None:
                return reference.strip()
            re_groups = result.groups()
            return re_groups[0]
        elif to:
            return to.split(KEYSEPARATOR)[0]
        else:
            raise Exception("Relation field without reference or to")


SIMPLE_TYPES = (
    "string",
    "number",
    "boolean",
    "JSON",
    "HTMLStrict",
    "HTMLPermissive",
    "float",
    "decimal(6)",
    "timestamp",
    "string[]",
    "number[]",
    "text[]",
    "text",
    "timezone",
)


TYPE_METHOD_MAP = {
    **{type_: GenerateCodeBlocks.get_schema_simple_types for type_ in SIMPLE_TYPES},
    "color": GenerateCodeBlocks.get_schema_color,
    "relation": GenerateCodeBlocks.get_relation_type,
    "relation-list": GenerateCodeBlocks.get_relation_list_type,
    "generic-relation": GenerateCodeBlocks.get_generic_relation_type,
    "generic-relation-list": GenerateCodeBlocks.get_generic_relation_list_type,
    "primary_key": GenerateCodeBlocks.get_schema_primary_key,
}


def main() -> None:
    """
    Main entry point for this script to generate the schema_relational.sql from the collections files.
    """

    _, checksum = InternalHelper.read_models_yml()

    (
        enum_definitions,
        pre_code,
        table_name_code,
        view_name_code,
        alter_table_code,
        final_info_code,
        missing_handled_attributes,
        missing_handled_collections_meta_attributes,
        im_table_code,
        create_trigger_partitioned_sequences_code,
        create_trigger_1_1_relation_not_null_code,
        create_trigger_1_n_relation_not_null_code,
        create_trigger_n_m_relation_not_null_code,
        create_trigger_prevent_updates_code,
        create_trigger_unique_ids_pair_code,
        create_trigger_equal_fields_code,
        create_trigger_notify_code,
        errors,
    ) = GenerateCodeBlocks.generate_the_code()
    with open(DESTINATION, "w") as dest:
        dest.write(Helper.FILE_TEMPLATE_HEADER)
        dest.write("-- MODELS_YML_CHECKSUM = " + repr(checksum) + "\n")
        dest.write("\n\n-- ENUM definitions\n")
        dest.write(enum_definitions)
        dest.write("\n\n-- Function and meta table definitions\n")
        dest.write(Helper.FILE_TEMPLATE_CONSTANT_DEFINITIONS)
        dest.write(pre_code)
        dest.write("\n\n-- Table definitions\n")
        dest.write(table_name_code)
        dest.write("\n\n-- Intermediate table definitions\n")
        dest.write(im_table_code)
        dest.write("\n\n-- View definitions\n")
        dest.write(view_name_code)
        dest.write("\n\n-- Alter table relations\n")
        dest.write(alter_table_code)
        dest.write("\n\n-- Create triggers generating partitioned sequences\n")
        dest.write(create_trigger_partitioned_sequences_code)
        dest.write(
            "\n\n-- Create triggers checking foreign_id not null for view-relations and no duplicates in 1:1 relationships\n"
        )
        dest.write(create_trigger_1_1_relation_not_null_code)
        dest.write(
            "\n\n-- Create triggers checking foreign_id not null for 1:n relationships\n"
        )
        dest.write(create_trigger_1_n_relation_not_null_code)
        dest.write(
            "\n\n-- Create triggers checking foreign_ids not null for n:m relationships\n"
        )
        dest.write(create_trigger_n_m_relation_not_null_code)
        dest.write("\n\n-- Create triggers for constant fields\n")
        dest.write(create_trigger_prevent_updates_code)
        dest.write(
            "\n\n-- Create triggers preventing mirrored duplicates in fields referencing themselves\n"
        )
        dest.write(create_trigger_unique_ids_pair_code)
        dest.write("\n\n-- Create triggers for notify\n")
        dest.write(create_trigger_notify_code)
        dest.write(
            "\n\n-- Create triggers checking equal_fields settings in relations\n"
        )
        dest.write(create_trigger_equal_fields_code)
        dest.write(Helper.RELATION_LIST_AGENDA)
        dest.write("/*\n")
        dest.write(final_info_code)
        dest.write("*/\n")
        if errors:
            dest.write(f"/*\nThere are {len(errors)} errors/warnings\n")
            dest.write("".join(errors))
            dest.write("*/\n")
        dest.write(
            f"\n/*   Missing attribute handling for {', '.join(missing_handled_attributes)} */"
        )
        if missing_handled_collections_meta_attributes:
            dest.write(
                f"\n/*   Missing handling for collections _meta attributes: {', '.join(missing_handled_collections_meta_attributes)} */"
            )
    if errors:
        print(f"Models file {DESTINATION} created with {len(errors)} errors/warnings\n")
        print("".join(errors))
    else:
        print(f"Models file {DESTINATION} successfully created.")


if __name__ == "__main__":
    main()
