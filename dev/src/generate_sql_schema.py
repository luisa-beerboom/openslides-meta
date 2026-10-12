import logging
import string
from collections import defaultdict
from collections.abc import Callable
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from string import Formatter
from textwrap import dedent, indent
from typing import Any, TypedDict, cast

from sqlfluff import fix

from .helper_get_names import (
    KEYSEPARATOR,
    FieldSqlErrorType,
    HelperGetNames,
    InternalHelper,
    TableFieldType,
)

DESTINATION = (Path(__file__).parent / ".." / "sql" / "schema_relational.sql").resolve()
MODELS: dict[str, dict[str, Any]] = {}


# Set log level for sqlfluff
for name in logging.root.manager.loggerDict:
    if "sqlfluff" in name:
        logging.getLogger(name).setLevel(logging.WARN)


class SchemaZoneTexts(TypedDict, total=False):
    """TypedDict definition for generation of different sql-code parts"""

    table: str
    view: str
    post_view: str
    alter_table: str
    alter_table_final: str
    create_trigger_partitioned_sequences: str
    create_trigger_1_1_relation_not_null: str
    create_trigger_1_n_relation_not_null: str
    create_trigger_n_m_relation_not_null: str
    create_trigger_prevent_updates_code: str
    create_trigger_unique_ids_pair_code: str
    create_trigger_equal_fields_code: str
    create_trigger_notify: str
    undecided: str
    final_info: str
    errors: list[str]


class SQL_Delete_Update_Options(StrEnum):
    RESTRICT = "RESTRICT"
    CASCADE = "CASCADE"
    SET_NULL = "SET NULL"
    SET_DEFAULT = "SET DEFAULT"
    NO_ACTION = "NO ACTION"


class SubstDict(TypedDict, total=False):
    """dict for substitutions of field templates"""

    field_name: str
    type: str
    primary_key: str
    required: str
    default: str
    minimum: str
    maximum: str
    minLength: str
    deferred: str
    check_enum: str
    check_timezone: str
    unique: str


class GenerateCodeBlocks:
    """Main work is done here by recursing the models and their fields and determine the method to use"""

    if not InternalHelper.MODELS:
        InternalHelper.read_models_yml()
    intermediate_tables: dict[str, str] = (
        {}
    )  # Key=Name, data: collected content of table

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

        for table_name, data in InternalHelper.MODELS.items():
            if table_name in ["_migration_index", "_meta"]:
                continue

            fields = data["fields"]
            schema_zone_texts = cast(SchemaZoneTexts, defaultdict(str))
            cls.intermediate_tables = {}

            for fname, fdata in fields.items():
                for attr in fdata:
                    if (
                        attr not in handled_attributes
                        and attr not in missing_handled_attributes
                    ):
                        missing_handled_attributes.append(attr)
                method_or_str, type_ = cls.get_method(fname, fdata)
                if isinstance(method_or_str, str):
                    error = Helper.prefix_error(method_or_str, table_name, fname)
                    schema_zone_texts["undecided"] += error
                    errors.append(error)
                else:
                    result, error = method_or_str(table_name, fname, fdata, type_)
                    for k, v in result.items():
                        schema_zone_texts[k] += v or ""  # type: ignore
                    if error:
                        errors.append(Helper.prefix_error(error, table_name, fname))

            if len(data) > 1:
                for attr, value in data.items():
                    match attr:
                        case "fields":
                            continue
                        case "unique_together":
                            schema_zone_texts[
                                "table"
                            ] += cls.get_constraint_unique_together(
                                table_name, value, False
                            )
                        case "unique_together_strict":
                            schema_zone_texts[
                                "table"
                            ] += cls.get_constraint_unique_together(
                                table_name, value, True
                            )
                        case _:
                            if attr not in collection_meta_handled_attributes:
                                missing_handled_collections_meta_attributes.add(attr)
                            else:
                                raise Exception(
                                    f"Attribute '{attr}' set to be handled but actually unhandled."
                                )

            if code := schema_zone_texts["table"]:
                table_name_code += Helper.get_table_head(table_name)
                table_name_code += Helper.get_table_body_end(code) + "\n\n"
            if code := schema_zone_texts["alter_table"]:
                table_name_code += code + "\n"
            if code := schema_zone_texts["undecided"]:
                table_name_code += Helper.get_undecided_all(table_name, code)
            view_name_code += Helper.get_view_head(table_name)
            view_name_code += Helper.get_view_body_end(
                table_name, schema_zone_texts.get("view", "")
            )
            if code := schema_zone_texts["post_view"]:
                view_name_code += code
            if code := schema_zone_texts["alter_table_final"]:
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
            for im_table in cls.intermediate_tables.values():
                im_table_code += im_table

            # schema_zone_texts is filled per model field.
            # If any fields for this collection generated table code, create the main notify trigger on it.
            if schema_zone_texts["table"]:
                create_trigger_notify_code += (
                    Helper.get_notify_trigger(table_name) + "\n"
                )
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
        if type_ in FIELD_TYPES:
            if method := FIELD_TYPES[type_].get("method"):
                return (method.__get__(cls), type_)  # returns the callable classmethod
            else:
                text = "no method defined"
        else:
            text = "Unknown Type"
        return (f"type:{type_} {text}\n", type_)

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
        if isinstance((tmp := subst["type"]), string.Template):
            if maxLength := fdata.get("maxLength"):
                tmp = tmp.substitute(
                    {
                        "maxLength": maxLength,
                        "field_name": fname,
                        "table_name": table_name,
                    }
                )
            elif isinstance(type_, Decimal):
                tmp = tmp.substitute(
                    {"maxLength": 6, "field_name": fname, "table_name": table_name}
                )
            elif isinstance(type_, str):  # string
                tmp = tmp.substitute(
                    {"maxLength": 256, "field_name": fname, "table_name": table_name}
                )
            subst["type"] = tmp
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
        tmpl = FIELD_TYPES[type_]["pg_type"]
        subst["type"] = tmpl.substitute(
            {"color_constraint": Helper.get_inline_color_constraint(table_name, fname)}
        )
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
            if cls.get_equal_fields(own_table_field, foreign_table_field):
                text["create_trigger_equal_fields_code"] = (
                    cls.get_trigger_check_equal_fields_for_1_x(
                        own_table_field, foreign_table_field, state
                    )
                )
            initially_deferred = fdata.get(
                "deferred"
            ) or ModelsHelper.is_fk_initially_deferred(table_name, foreign_table)
            text["alter_table_final"] = (
                Helper.get_foreign_key_table_constraint_as_alter_table(
                    table_name,
                    foreign_table,
                    fname,
                    foreign_table_field.ref_column,
                    initially_deferred,
                )
            )
            table_name = HelperGetNames.get_table_name(table_name)
            text["create_trigger_notify"] = Helper.get_foreign_key_notify_trigger(
                table_name,
                foreign_table_field.table,
                fname,
                foreign_table_field.column,
                foreign_table_field.ref_column,
                initially_deferred,
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
                    if cls.get_equal_fields(own_table_field, foreign_table_field):
                        text["create_trigger_equal_fields_code"] = (
                            cls.get_trigger_check_equal_fields_for_n_m(
                                own_table_field,
                                foreign_table_field,
                                nm_table_name,
                                own_intermediate_field,
                                foreign_intermediate_field,
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
            fields = [field_name.strip() for field_name in fields.split(",")]
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
    def get_equal_field_trigger_config(
        cls, table_field: TableFieldType, fields: list[TableFieldType | str]
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

    @classmethod
    def get_event_string(cls, on_update_fields: list[str]) -> str:
        if on_update_fields:
            return f"INSERT OR UPDATE OF {', '.join(on_update_fields)}"
        else:
            return "INSERT"

    @classmethod
    def get_trigger_check_equal_fields_for_1_x(
        cls,
        own_table_field: TableFieldType,
        foreign_table_field: TableFieldType,
        state: FieldSqlErrorType,
    ) -> str:
        cls.equal_fields_state_check(state, own_table_field)
        equal_fields = cls.get_equal_fields(own_table_field, foreign_table_field)
        sql = ""
        for equal_field in equal_fields:
            own_table, own_on_update_fields = cls.get_equal_field_trigger_config(
                own_table_field, [own_table_field, equal_field]
            )
            own_event_str = cls.get_event_string(own_on_update_fields)
            foreign_table, foreign_on_update_fields = (
                cls.get_equal_field_trigger_config(foreign_table_field, [equal_field])
            )
            if (
                "reference" in own_table_field.field_def
                and "reference" in foreign_table_field.field_def
                and foreign_table_field.field_def.get("type") in ["relation"]
            ):
                raise Exception(
                    f"Cannot generate equal_fields triggers for {own_table_field.collectionfield} and {foreign_table_field.collectionfield}: Both have reference set."
                )
            own_trigger_name = HelperGetNames.get_equal_field_trigger_name(
                equal_field, own_table, own_table_field.column
            )
            foreign_event_str = cls.get_event_string(foreign_on_update_fields)
            foreign_trigger_name = HelperGetNames.get_equal_field_trigger_name(
                equal_field, foreign_table, foreign_table_field.column
            )
            sql += dedent(f"""
                CREATE CONSTRAINT TRIGGER {own_trigger_name} AFTER {own_event_str} ON {own_table} INITIALLY DEFERRED
                FOR EACH ROW EXECUTE FUNCTION check_equals('{own_table_field.table}', '{foreign_table_field.table}', '{own_table_field.column}', '{equal_field}', FALSE);
                CREATE CONSTRAINT TRIGGER {foreign_trigger_name} AFTER {foreign_event_str} ON {foreign_table} INITIALLY DEFERRED
                FOR EACH ROW EXECUTE FUNCTION check_equals('{own_table_field.table}', '{foreign_table_field.table}', '{own_table_field.column}', '{equal_field}', TRUE);

            """)
        return sql

    @classmethod
    def get_trigger_check_equal_fields_for_n_m(
        cls,
        own_table_field: TableFieldType,
        foreign_table_field: TableFieldType,
        nm_table_name: str,
        own_intermediate_field: str,
        foreign_intermediate_field: str,
    ) -> str:
        equal_fields = cls.get_equal_fields(own_table_field, foreign_table_field)
        sql = ""
        for equal_field in equal_fields:
            own_table, own_on_update_fields = cls.get_equal_field_trigger_config(
                own_table_field, [equal_field]
            )
            own_event_str = cls.get_event_string(own_on_update_fields)
            foreign_table, foreign_on_update_fields = (
                cls.get_equal_field_trigger_config(foreign_table_field, [equal_field])
            )
            foreign_event_str = cls.get_event_string(foreign_on_update_fields)
            intermediate_event_str = cls.get_event_string([])
            own_trigger_name = HelperGetNames.get_equal_field_trigger_name(
                equal_field, own_table, own_table_field.column
            )
            foreign_trigger_name = HelperGetNames.get_equal_field_trigger_name(
                equal_field, foreign_table, foreign_table_field.column
            )
            intermediate_trigger_name = (
                HelperGetNames.get_equal_field_intermediate_trigger_name(
                    equal_field, own_table, own_table_field.column
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
    def get_trigger_check_equal_fields_for_g1_x(
        cls,
        own_table_field: TableFieldType,
        foreign_table_field: TableFieldType,
        specified_relation_field: str,
        state: FieldSqlErrorType,
    ) -> str:
        cls.equal_fields_state_check(state, own_table_field)
        equal_fields = cls.get_equal_fields(own_table_field, foreign_table_field)
        sql = ""
        for equal_field in equal_fields:
            own_table, own_on_update_fields = cls.get_equal_field_trigger_config(
                own_table_field, [own_table_field, equal_field]
            )
            own_event_str = cls.get_event_string(own_on_update_fields)
            foreign_table, foreign_on_update_fields = (
                cls.get_equal_field_trigger_config(foreign_table_field, [equal_field])
            )
            own_trigger_name = HelperGetNames.get_equal_field_trigger_name(
                equal_field, own_table, specified_relation_field
            )
            if (
                foreign_table_field.table == "meeting"
                and equal_field == "meeting_id"
                and "meeting_id" not in InternalHelper.MODELS["meeting"]["fields"]
            ):
                sql += dedent(f"""
                    CREATE CONSTRAINT TRIGGER {own_trigger_name} AFTER {own_event_str} ON {own_table} INITIALLY DEFERRED
                    FOR EACH ROW EXECUTE FUNCTION check_equals_meeting_id_for_meeting('{own_table_field.table}', '{specified_relation_field}');

                """)
            else:
                foreign_event_str = cls.get_event_string(foreign_on_update_fields)
                foreign_trigger_name = HelperGetNames.get_equal_field_trigger_name(
                    equal_field, foreign_table, foreign_table_field.column
                )
                sql += dedent(f"""
                    CREATE CONSTRAINT TRIGGER {own_trigger_name} AFTER {own_event_str} ON {own_table} INITIALLY DEFERRED
                    FOR EACH ROW EXECUTE FUNCTION check_equals('{own_table_field.table}', '{foreign_table_field.table}', '{specified_relation_field}', '{equal_field}', FALSE);
                    CREATE CONSTRAINT TRIGGER {foreign_trigger_name} AFTER {foreign_event_str} ON {foreign_table} INITIALLY DEFERRED
                    FOR EACH ROW EXECUTE FUNCTION check_equals('{own_table_field.table}', '{foreign_table_field.table}', '{specified_relation_field}', '{equal_field}', TRUE);

                """)
        return sql

    @classmethod
    def get_trigger_check_equal_fields_for_gn_m(
        cls,
        own_table_field: TableFieldType,
        foreign_table_field: TableFieldType,
        nm_table_name: str,
        own_intermediate_field: str,
        foreign_intermediate_field: str,
    ) -> str:
        equal_fields = cls.get_equal_fields(own_table_field, foreign_table_field)
        sql = ""
        for equal_field in equal_fields:
            own_table, own_on_update_fields = cls.get_equal_field_trigger_config(
                own_table_field, [equal_field]
            )
            own_event_str = cls.get_event_string(own_on_update_fields)
            foreign_table, foreign_on_update_fields = (
                cls.get_equal_field_trigger_config(foreign_table_field, [equal_field])
            )
            foreign_event_str = cls.get_event_string(foreign_on_update_fields)
            intermediate_event_str = cls.get_event_string([])
            own_trigger_name = HelperGetNames.get_equal_field_trigger_name(
                equal_field, own_table, own_table_field.column, foreign_table
            )
            foreign_trigger_name = HelperGetNames.get_equal_field_trigger_name(
                equal_field, foreign_table, foreign_table_field.column
            )
            intermediate_trigger_name = (
                HelperGetNames.get_equal_field_intermediate_trigger_name(
                    equal_field, own_table, own_table_field.column, foreign_table
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
        cls, table_name: str, fname: str, fdata: dict[str, Any], type_: str
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
            initially_deferred = any(
                ModelsHelper.is_fk_initially_deferred(
                    table_name, foreign_table_field.table
                )
                for foreign_table_field in foreign_table_fields
            )
            foreign_tables: list[str] = []
            equal_fields_text = ""
            for foreign_table_field in foreign_table_fields:
                generic_plain_field_name = f"{own_table_field.column}_{foreign_table_field.table}_{foreign_table_field.ref_column}"
                foreign_tables.append(foreign_table_field.table)
                text["table"] += Helper.get_generic_combined_fields(
                    table_name,
                    generic_plain_field_name,
                    own_table_field.column,
                    foreign_table_field,
                )
                if cls.get_equal_fields(own_table_field, foreign_table_field):
                    equal_fields_text += cls.get_trigger_check_equal_fields_for_g1_x(
                        own_table_field,
                        foreign_table_field,
                        generic_plain_field_name,
                        state,
                    )
                text[
                    "create_trigger_notify"
                ] += Helper.get_trigger_for_generic_relation(
                    table_name,
                    generic_plain_field_name,
                    foreign_table_field.column,
                    foreign_table_field.table,
                )
                text[
                    "alter_table_final"
                ] += Helper.get_foreign_key_table_constraint_as_alter_table(
                    own_table_field.table,
                    foreign_table_field.table,
                    generic_plain_field_name,
                    foreign_table_field.ref_column,
                    initially_deferred,
                )
            if equal_fields_text:
                text["create_trigger_equal_fields_code"] = equal_fields_text
            text["table"] += Helper.get_generic_field_constraint(
                own_table_field.table, own_table_field.column, foreign_tables
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
                    if cls.get_equal_fields(own_table_field, foreign_table_field):
                        equal_fields_text += (
                            cls.get_trigger_check_equal_fields_for_gn_m(
                                own_table_field,
                                foreign_table_field,
                                gm_foreign_table,
                                own_intermediate_field,
                                foreign_intermediate_field,
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
    def get_foreign_key_table_constraint_as_alter_table(
        table_name: str,
        foreign_table: str,
        own_column: str,
        fk_column: str,
        initially_deferred: bool = False,
        delete_action: str = "",
        update_action: str = "",
    ) -> str:
        FOREIGN_KEY_TABLE_CONSTRAINT_TEMPLATE = string.Template(
            "ALTER TABLE ${own_table} ADD CONSTRAINT ${fk_name} FOREIGN KEY(${own_column}) REFERENCES ${foreign_table}(${fk_column})${initially_deferred}${delete_action}${update_action};\n"
            "CREATE INDEX ${index} ON ${own_table} (${own_column});\n"
        )

        if initially_deferred:
            text_initially_deferred = " INITIALLY DEFERRED"
        else:
            text_initially_deferred = ""
        own_table = HelperGetNames.get_table_name(table_name)
        foreign_table = HelperGetNames.get_table_name(foreign_table)
        fk_idx = HelperGetNames.get_fk_and_index_name(
            own_table, own_column, foreign_table, fk_column
        )
        result = FOREIGN_KEY_TABLE_CONSTRAINT_TEMPLATE.substitute(
            {
                "own_table": own_table,
                "fk_name": fk_idx[0],
                "index": fk_idx[1],
                "foreign_table": foreign_table,
                "own_column": own_column,
                "fk_column": fk_column,
                "initially_deferred": text_initially_deferred,
                "delete_action": Helper.get_on_action_mode(delete_action, True),
                "update_action": Helper.get_on_action_mode(update_action, False),
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
            on_table = log_trigger["on_table"]
            on_columns = log_trigger.get("on_columns")

            if on_table not in processed_tables:
                processed_tables[on_table] = 1
                unique_index = None
            else:
                processed_tables[on_table] += 1
                unique_index = processed_tables[on_table]

            trigger_columns_iu = f" OR UPDATE OF {on_columns}" if on_columns else ""
            trigger_columns_ud = f" UPDATE OF {on_columns} OR" if on_columns else ""
            trigger_name_iu, trigger_name_ud = (
                HelperGetNames.get_log_calculated_id_array_trigger_names(
                    view_name, log_field, on_table, bool(on_columns), unique_index
                )
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
                "on_table": on_table,
            }
            subst_iu = {
                **subst_common,
                "trigger_type": "iu",
                "trigger_name": trigger_name_iu,
                "trigger_operations": f"BEFORE INSERT{trigger_columns_iu}",
            }

            subst_ud = {
                **subst_common,
                "trigger_type": "ud",
                "trigger_name": trigger_name_ud,
                "trigger_operations": f"AFTER{trigger_columns_ud} DELETE",
            }

            parts.append(TRIGGER_TEMPLATE.substitute(subst_iu))
            parts.append(TRIGGER_TEMPLATE.substitute(subst_ud))

        return "".join(parts)

    @staticmethod
    def get_nm_table_for_n_m_relation_lists(
        own_table_field: TableFieldType, foreign_table_field: TableFieldType
    ) -> tuple[str, str, str, str]:
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
    def get_gm_table_for_gm_nm_relation_lists(
        own_table_field: TableFieldType, foreign_table_fields: list[TableFieldType]
    ) -> tuple[str, str, str, dict[str, TableFieldType]]:
        gm_table_name = HelperGetNames.get_gm_table_name(own_table_field)
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
        foreign_table_ref_lines = []
        indices_lines = []
        own_table_column = own_table_field.intermediate_column
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

        own_table_name = HelperGetNames.get_table_name(own_table_field.table)
        own_table_name_with_ref_column = (
            HelperGetNames.get_own_table_name_with_ref_column(own_table_field)
        )
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
        subst_type = enum_type or FIELD_TYPES[type_]["pg_type"]
        subst.update({"field_name": fname, "type": subst_type})
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
            if isinstance(default, str) or type_ in ("string", "text", "timezone"):
                default_value = f"'{default}'"
            elif isinstance(default, (int, bool, float)):
                default_value = str(default)
            elif isinstance(default, list):
                default_value = (
                    '{"' + '", "'.join(default) + '"}' if default else "'{}'"
                )
            else:
                raise Exception(
                    f"{table_name}.{fname}: seems to be an invalid default value"
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

        return f"    {generic_plain_field_name} integer{unique}{generated_always_as},\n"

    @staticmethod
    def get_generic_field_constraint(
        collection: str, own_column: str, foreign_tables: list[str]
    ) -> str:
        constraint_name = HelperGetNames.get_generic_valid_constraint_name(
            collection, own_column
        )
        return f"""    CONSTRAINT {constraint_name} CHECK (split_part({own_column}, '/', 1) IN ('{"','".join(foreign_tables)}')),\n"""

    @staticmethod
    def prefix_error(method_or_str: str, table_name: str, fname: str) -> str:
        return f"    {table_name}/{fname}: {method_or_str}"


class ModelsHelper:
    @staticmethod
    def is_fk_initially_deferred(own_table: str, foreign_table: str) -> bool:
        """
        The "Initially deferred" in fk-definition is necessary,
        if 2 related tables require both the relation to the other table
        """

        def _first_to_second(t1: str, t2: str) -> bool:
            for field in InternalHelper.MODELS[t1].values():
                if field.get("required") and field["type"].startswith("relation"):
                    ftable = ModelsHelper.get_foreign_table_from_to_or_reference(
                        field.get("to"), field.get("reference")
                    )
                    if ftable == t2:
                        return True
            return False

        return True
        # TODO: Will be reverted in a future issue
        # if _first_to_second(own_table, foreign_table):
        #     return _first_to_second(foreign_table, own_table)
        # return False

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


FIELD_TYPES: dict[str, dict[str, Any]] = {
    "string": {
        "pg_type": string.Template("varchar(${maxLength})"),
        "method": GenerateCodeBlocks.get_schema_simple_types,
    },
    "number": {
        "pg_type": "integer",
        "method": GenerateCodeBlocks.get_schema_simple_types,
    },
    "boolean": {
        "pg_type": "boolean",
        "method": GenerateCodeBlocks.get_schema_simple_types,
    },
    "JSON": {"pg_type": "jsonb", "method": GenerateCodeBlocks.get_schema_simple_types},
    "HTMLStrict": {
        "pg_type": "text",
        "method": GenerateCodeBlocks.get_schema_simple_types,
    },
    "HTMLPermissive": {
        "pg_type": "text",
        "method": GenerateCodeBlocks.get_schema_simple_types,
    },
    "float": {
        "pg_type": "double precision",
        "method": GenerateCodeBlocks.get_schema_simple_types,
    },
    "decimal(6)": {
        "pg_type": "decimal(16,6)",
        "method": GenerateCodeBlocks.get_schema_simple_types,
    },
    "timestamp": {
        "pg_type": "timestamptz",
        "method": GenerateCodeBlocks.get_schema_simple_types,
    },
    "color": {
        "pg_type": string.Template("varchar(7)${color_constraint}"),
        "method": GenerateCodeBlocks.get_schema_color,
    },
    "string[]": {
        "pg_type": string.Template("varchar(${maxLength})[]"),
        "method": GenerateCodeBlocks.get_schema_simple_types,
    },
    "number[]": {
        "pg_type": "integer[]",
        "method": GenerateCodeBlocks.get_schema_simple_types,
    },
    "text[]": {
        "pg_type": "text[]",
        "method": GenerateCodeBlocks.get_schema_simple_types,
    },
    "text": {"pg_type": "text", "method": GenerateCodeBlocks.get_schema_simple_types},
    "timezone": {
        "pg_type": "text",
        "method": GenerateCodeBlocks.get_schema_simple_types,
    },
    "relation": {"pg_type": "integer", "method": GenerateCodeBlocks.get_relation_type},
    "relation-list": {
        "pg_type": "integer[]",
        "method": GenerateCodeBlocks.get_relation_list_type,
    },
    "generic-relation": {
        "pg_type": "varchar(100)",
        "method": GenerateCodeBlocks.get_generic_relation_type,
    },
    "generic-relation-list": {
        "pg_type": "varchar(100)[]",
        "method": GenerateCodeBlocks.get_generic_relation_list_type,
    },
    # special defined
    "primary_key": {
        "pg_type": "integer",
        "method": GenerateCodeBlocks.get_schema_primary_key,
    },
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
