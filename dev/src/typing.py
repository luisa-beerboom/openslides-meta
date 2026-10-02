import string
from enum import Enum
from typing import Literal, TypedDict

TriggerSqlDict = dict[str, dict[str, dict[str, str]]]

SchemaZoneKey = Literal[
    "table",
    "alter_table",
    "alter_table_final",
    "view",
    "post_view",
    "create_trigger_partitioned_sequences",
    "create_trigger_1_1_relation_not_null",
    "create_trigger_1_n_relation_not_null",
    "create_trigger_n_m_relation_not_null",
    "create_trigger_prevent_updates_code",
    "create_trigger_unique_ids_pair_code",
    "create_trigger_equal_fields_code",
    "create_trigger_notify",
    "undecided",
    "final_info",
    "errors",
]


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


class SQL_Delete_Update_Options(str, Enum):
    RESTRICT = "RESTRICT"
    CASCADE = "CASCADE"
    SET_NULL = "SET NULL"
    SET_DEFAULT = "SET DEFAULT"
    NO_ACTION = "NO ACTION"


class SubstDict(TypedDict, total=False):
    """dict for substitutions of field templates"""

    field_name: str
    type: str | string.Template
    primary_key: str
    required: str
    default: str
    minimum: str
    maximum: str
    minLength: str
    check_enum: str
    check_timezone: str
    unique: str


PG_TYPES: dict[str, str | string.Template] = {
    "string": string.Template("varchar(${maxLength})"),
    "number": "integer",
    "boolean": "boolean",
    "JSON": "jsonb",
    "HTMLStrict": "text",
    "HTMLPermissive": "text",
    "float": "double precision",
    "decimal(6)": "decimal(16,6)",
    "timestamp": "timestamptz",
    "color": string.Template("varchar(7)${color_constraint}"),
    "string[]": string.Template("varchar(${maxLength})[]"),
    "number[]": "integer[]",
    "text[]": "text[]",
    "text": "text",
    "timezone": "text",
    "relation": "integer",
    "relation-list": "integer[]",
    "generic-relation": "varchar(100)",
    "generic-relation-list": "varchar(100)[]",
    "primary_key": "integer",
}
