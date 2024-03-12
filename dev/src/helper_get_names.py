import hashlib
import os
import re
from collections.abc import Callable
from typing import Any

import requests
import yaml

KEYSEPARATOR = "/"


class TableFieldType:
    def __init__(
        self,
        table: str,
        column: str,
        field_def: dict[str, Any] | None,
        ref_column: str = "id",
    ):
        self.table = table
        self.column = column
        self.field_def: dict[str, Any] = field_def or {}
        self.ref_column = ref_column

    @property
    def collectionfield(self) -> str:
        if self.table:
            return f"{self.table}{KEYSEPARATOR}{self.column}"
        else:
            return "-"

    @staticmethod
    def get_definitions_from_foreign(
        to: str | None, reference: str | None
    ) -> "TableFieldType":
        tname = ""
        fname = ""
        tfield: dict[str, Any] = {}
        ref_column = ""
        if to:
            tname, fname, tfield = InternalHelper.get_field_definition_from_to(to)
            ref_column = "id"
        if reference:
            tname, ref_column = InternalHelper.get_foreign_key_table_column(
                to, reference
            )
        return TableFieldType(tname, fname, tfield, ref_column)


class HelperGetNames:
    MAX_LEN = 63
    trigger_unique_list: list[str] = []

    @staticmethod
    def max_length(func: Callable) -> Callable:
        def wrapper(*args, **kwargs) -> str:  # type:ignore
            name = func(*args, **kwargs)
            assert (
                len(name) <= HelperGetNames.MAX_LEN
            ), f"Generated name '{name}' to long in function {func}!"
            return name

        return wrapper

    @staticmethod
    @max_length
    def get_table_name(table_name: str) -> str:
        """get's the table name as ol dcollection name with appendis 'T'"""
        return table_name + "T"

    @staticmethod
    @max_length
    def get_view_name(table_name: str) -> str:
        """get's the name of a view, usually the old collection name"""
        if table_name in ("group", "user"):
            return table_name + "_"
        return table_name

    @staticmethod
    @max_length
    def get_nm_table_name(own: TableFieldType, foreign: TableFieldType) -> str:
        """get's the table name n:m-relations intermediate table"""
        if (own_str := f"{own.table}_{own.column}") < (
            foreign_str := f"{foreign.table}_{foreign.column}"
        ):
            return f"nm_{own_str}_{foreign.table}"
        else:
            return f"nm_{foreign_str}_{own.table}"

    @staticmethod
    @max_length
    def get_gm_table_name(table_field: TableFieldType) -> str:
        """get's th table name for generic-list:many-relations intermediate table"""
        return f"gm_{table_field.table}_{table_field.column}"

    @staticmethod
    @max_length
    def get_field_in_n_m_relation_list(
        own_table_field: TableFieldType, foreign_table_name: str
    ) -> str:
        """get's the field name in a n:m-intermediate table.
        If both sides of the relation are in same table, the field name without 's' is used,
        otherwise the related tables names are used
        """
        if own_table_field.table == foreign_table_name:
            return own_table_field.column[:-1]
        else:
            return f"{own_table_field.table}_id"

    @staticmethod
    @max_length
    def get_gm_content_field(table: str, field: str) -> str:
        """Gets the name of content field in an generic:many intermediate table"""
        return f"{table}_{field}_id"

    @staticmethod
    @max_length
    def get_enum_type_name(
        fname: str,
        table_name: str,
    ) -> str:
        """gets the name of an enum with prefix enum, table_name_name and fname"""
        return f"enum_{table_name}_{fname}"

    @staticmethod
    @max_length
    def get_generic_valid_constraint_name(
        fname: str,
    ) -> str:
        """gets the name of a generic valid constraint"""
        return f"valid_{fname}_part1"

    @staticmethod
    @max_length
    def get_generic_unique_constraint_name(
        own_table_name_with_ref_column: str, own_table_column: str
    ) -> str:
        """gets the name of a generic unique constraint
        Params:
        - {table_name}_{ref_column}
        - {owcolumn}
        """
        return f"unique_${own_table_name_with_ref_column}_${own_table_column}"

    @staticmethod
    @max_length
    def get_minimum_constraint_name(
        fname: str,
    ) -> str:
        """gets the name of minimum constraint"""
        return f"minimum_{fname}"

    @staticmethod
    @max_length
    def get_minlength_constraint_name(
        fname: str,
    ) -> str:
        """gets the name of minLength constraint"""
        return f"minlength_{fname}"

    @staticmethod
    @max_length
    def get_not_null_rel_list_insert_trigger_name(
        table_name: str,
        column_name: str,
    ) -> str:
        """gets the name of the insert trigger for not null on relation lists"""
        name = f"tr_i_{table_name}_{column_name}"[: HelperGetNames.MAX_LEN]
        if name in HelperGetNames.trigger_unique_list:
            raise Exception(f"trigger {name} is not unique!")
        HelperGetNames.trigger_unique_list.append(name)
        return name

    @staticmethod
    @max_length
    def get_not_null_rel_list_upd_del_trigger_name(
        table_name: str,
        column_name: str,
    ) -> str:
        """gets the name of the update/delete trigger for not null on relation lists"""
        name = f"tr_ud_{table_name}_{column_name}"[: HelperGetNames.MAX_LEN]
        if name in HelperGetNames.trigger_unique_list:
            raise Exception(f"trigger {name} is not unique!")
        HelperGetNames.trigger_unique_list.append(name)
        return name


class InternalHelper:
    MODELS: dict[str, dict[str, Any]] = {}
    checksum: str = ""
    ref_compiled = compiled = re.compile(r"(^\w+\b).*?\((.*?)\)")

    @classmethod
    def read_models_yml(cls, file: str) -> tuple[dict[str, Any], str]:
        """method reads modesl.yml from file or web and returns MODELS and it's checksum"""
        if os.path.isfile(file):
            with open(file, "rb") as x:
                models_yml = x.read()
        else:
            models_yml = requests.get(file).content

        # calc checksum to assert the schema.sql is up-to-date
        checksum = hashlib.md5(models_yml).hexdigest()

        # Fix broken keys
        models_yml = models_yml.replace(b" yes:", b' "yes":')
        models_yml = models_yml.replace(b" no:", b' "no":')

        # Load and parse models.yml
        cls.MODELS = yaml.safe_load(models_yml)
        cls.check_field_length()
        return cls.MODELS, checksum

    @classmethod
    def check_field_length(cls) -> None:
        to_long: list[str] = []
        for table_name, fields in cls.MODELS.items():
            if table_name in ["_migration_index", "_meta"]:
                continue
            for fname in fields.keys():
                if len(fname) > HelperGetNames.MAX_LEN:
                    to_long.append(f"{table_name}.{fname}:{len(fname)}")
        if to_long:
            raise Exception("\n".join(to_long))

    @staticmethod
    def get_field_definition_from_to(to: str) -> tuple[str, str, dict[str, Any]]:
        tname, fname = to.split("/")
        try:
            field = InternalHelper.get_models(tname, fname)
        except Exception:
            raise Exception(
                f"Exception on splitting to {to} in get_field_definition_from_to"
            )
        assert (
            len(tname) <= HelperGetNames.MAX_LEN
        ), f"Generated tname '{tname}' to long in function 'get_field_definition_from_to'!"
        assert (
            len(fname) <= HelperGetNames.MAX_LEN
        ), f"Generated fname '{fname}' to long in function 'get_field_definition_from_to'!"

        return tname, fname, field

    @staticmethod
    def get_foreign_key_table_column(
        to: str | None, reference: str | None
    ) -> tuple[str, str]:
        """
        Returns a tuple (table_name, field_name) gotten from "to" and/or "reference"-attribut
        """
        if reference:
            result = InternalHelper.ref_compiled.search(reference)
            if result is None:
                return reference.strip(), "id"
            re_groups = result.groups()
            cols = re_groups[1]
            if cols:
                cols = ",".join([col.strip() for col in cols.split(",")])
            else:
                cols = "id"
            return re_groups[0], cols
        elif to:
            return to.split("/")[0], "id"
        else:
            raise Exception("Relation field without reference or to")

    @classmethod
    def get_models(cls, collection: str, field: str) -> dict[str, Any]:
        if cls.MODELS:
            try:
                return cls.MODELS[collection][field]
            except KeyError:
                raise Exception(f"MODELS field {collection}.{field} doesn't exist")
        raise Exception("You have to initialize models in class InternalHelper")
