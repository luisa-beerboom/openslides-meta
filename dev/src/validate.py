import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, cast

import simplejson as json
import yaml

from .helper_get_names import (
    DEFAULT_COLLECTION_META,
    DEFAULT_COLLECTIONS_DIR,
    KEYSEPARATOR,
    PERMISSIONS_SOURCE,
)

MAX_FIELD_NAME_LENGTH = 63

_collection_regex = r"[a-z](?:[a-z_]+[a-z]+)?"
_field_regex = r"[a-z][a-z0-9_]*"

COLLECTION_REGEX = re.compile(f"^{_collection_regex}$")
FIELD_REGEX = re.compile(f"^{_field_regex}$")
COLLECTIONFIELD_REGEX = re.compile(f"^{_collection_regex}{KEYSEPARATOR}{_field_regex}$")

DECIMAL_REGEX = re.compile(r"^-?(\d|[1-9]\d+)\.\d{6}$")
COLOR_REGEX = re.compile(r"^#[0-9a-f]{6}$")


RELATION_TYPES = (
    "relation",
    "relation-list",
    "generic-relation",
    "generic-relation-list",
)

DATA_TYPES = (
    "string",
    "number",
    "string[]",
    "number[]",
    "text[]",
    "boolean",
    "JSON",
    "HTMLStrict",
    "HTMLPermissive",
    "float",
    "decimal(6)",
    "timestamp",
    "color",
    "text",
    "text[]",
    "timezone",
)


VALID_TYPES = DATA_TYPES + RELATION_TYPES

OPTIONAL_ATTRIBUTES = (
    "description",
    "calculated",
    "required",
    "read_only",
    "constant",
    "constant_legacy",
    "unique",
    "sequence_scope",
)


class CheckException(Exception):
    pass


class Checker:
    def __init__(self, collections_dir: str) -> None:
        self.models: dict[str, Any] = {}
        self.meta_data: dict[str, Any] = defaultdict(dict)
        self.errors: list[str] = []
        self._load_collections(collections_dir)

    def _load_collections(self, collections_dir: str) -> None:
        meta_path = Path(DEFAULT_COLLECTION_META)
        collections_path = Path(collections_dir)

        with open(meta_path, "rb") as f:
            self.shared_enum_definitions = yaml.safe_load(f.read()).get(
                "enum_definitions", {}
            )

        if not collections_path.exists():
            raise CheckException(
                f"Collections directory '{collections_dir}' does not exist."
            )

        if not collections_path.is_dir():
            raise CheckException(f"'{collections_dir}' is not a directory.")

        yaml_files = sorted(collections_path.glob("*.yml")) + sorted(
            collections_path.glob("*.yaml")
        )

        if not yaml_files:
            raise CheckException(f"No YAML files found in '{collections_dir}'.")

        for yaml_file in yaml_files:
            try:
                with open(yaml_file, "rb") as f:
                    data = yaml.safe_load(f.read())

                if not isinstance(data, dict):
                    self.errors.append(
                        f"File '{yaml_file.name}' does not contain a valid dictionary."
                    )
                    continue

                for attr, value in data.items():
                    if attr == "fields":
                        self.models[yaml_file.stem] = value
                    else:
                        self.meta_data[yaml_file.stem][attr] = value

            except yaml.YAMLError as e:
                self.errors.append(f"Error parsing '{yaml_file.name}': {e}")
            except Exception as e:
                self.errors.append(f"Error reading '{yaml_file.name}': {e}")

    def run_check(self) -> None:
        self._run_checks()
        if self.errors:
            errors = [f"\t{error}" for error in self.errors]
            raise CheckException("\n".join(errors))

    def _run_checks(self) -> None:
        for collection in list(self.models.keys()):
            if collection.startswith("_"):
                self.models.pop(collection)
            elif not COLLECTION_REGEX.match(collection):
                self.errors.append(f"Collection '{collection}' is not valid.")
        if self.errors:
            return

        for collection, fields in self.models.items():
            if not isinstance(fields, dict):
                self.errors.append(
                    f"The fields of collection {collection} must be a dict."
                )
                continue
            for field_name, field in fields.items():
                if not FIELD_REGEX.match(field_name):
                    self.errors.append(
                        f"Field name '{field_name}' of collection {collection} is not a valid field name."
                    )
                    continue
                if not isinstance(field, dict):
                    self.errors.append(
                        f"Field '{field_name}' of collection {collection} must be a dict."
                    )
                self.check_field(collection, field_name, field)

        if self.errors:
            return

        for collection, fields in self.models.items():
            for field_name, field in fields.items():
                is_relation_field = field["type"] in RELATION_TYPES
                if not is_relation_field:
                    continue
                error = self.check_relation(collection, field_name, field)
                if error:
                    self.errors.append(error)
        for collection, data in self.meta_data.items():
            for attr, values in data.items():
                if attr in ["unique_together", "unique_together_strict"]:
                    self.check_unique_together(collection, values, attr)
        self.check_permissions()

    def check_field(
        self,
        collection: str,
        field_name: str,
        field: str | dict[str, Any],
        nested: bool = False,
    ) -> None:
        if len(field_name) > MAX_FIELD_NAME_LENGTH:
            self.errors.append(
                f"Field name '{field_name}' for collection {collection} is longer than the maximum {MAX_FIELD_NAME_LENGTH} characters."
            )
            return

        collectionfield = f"{collection}{KEYSEPARATOR}{field_name}"

        if isinstance(field, str):
            field = cast(dict[str, Any], {"type": field})

        if nested:
            field["restriction_mode"] = (
                "A"  # add restriction_mode to satisfy the checker below.
            )

        type = field.get("type")
        if type not in VALID_TYPES:
            self.errors.append(
                f"Type '{type}' for collectionfield {collectionfield} is invalid."
            )
            return

        required_attributes = [
            "type",
            "restriction_mode",
        ]
        if type in RELATION_TYPES:
            required_attributes.append("to")
        for attr in required_attributes:
            if attr not in field:
                self.errors.append(
                    f"Required attribute '{attr}' for collectionfield {collectionfield} is missing."
                )
                return

        if field.get("calculated"):
            return

        if scope_field_name := field.get("sequence_scope", ""):
            if type != "number":
                self.errors.append(
                    f"Sequences can only be generated for number fields. {collectionfield} is {type}."
                )
            if scope_field_name not in self.models[collection]:
                self.errors.append(
                    f"{scope_field_name} can not be used as a source of sequence scope since it is not part of {collection}."
                )

        valid_attributes = list(OPTIONAL_ATTRIBUTES) + required_attributes
        if type in ["string[]", "text[]"]:
            valid_attributes.append("items")
            if "items" in field:
                if "enum" in field["items"]:
                    if shared_enum := self.validate_enum(
                        collectionfield, field["items"]["enum"]
                    ):
                        field["items"]["enum"] = shared_enum

                    if "default" in field:
                        if isinstance(field["items"]["enum"], list) and (
                            invalid_values := [
                                item
                                for item in field["default"]
                                if item not in field["items"]["enum"]
                            ]
                        ):
                            self.errors.append(
                                f"some default values for {collectionfield} are not "
                                f"found in 'enum' for the field: {invalid_values}. "
                                f"Allowed values are: {field['items']['enum']}."
                            )

                else:
                    self.errors.append(
                        f"'items' is missing an inner 'enum' for {collectionfield}"
                    )
        if type == "JSON" and "default" in field:
            try:
                json.loads(json.dumps(field["default"]))
            except:  # NOQA
                self.errors.append(
                    f"Default value for {collectionfield}' is not valid json."
                )
        if type in ("number", "float", "decimal(6)"):
            valid_attributes.extend(["minimum", "maximum"])
            for attr in ["minimum", "maximum"]:
                if attr in field:
                    self.validate_value_for_type(type, field[attr], collectionfield)
            if "minimum" in field and "maximum" in field:
                if field["minimum"] > field["maximum"]:
                    self.errors.append(
                        f"Incorrect 'maximum' and 'minimum' values for {collectionfield}: 'maximum' ({field['maximum']}) must be bigger or equal to 'minimum' ({field['minimum']})."
                    )
            if "default" in field:
                base_error_message = f"incorrect 'default' value for {collectionfield}: {field['default']}. Allowed"
                for attr, comparison_func in (
                    ("minimum", lambda a, b: a < b),
                    ("maximum", lambda a, b: a > b),
                ):
                    if attr in field and comparison_func(field["default"], field[attr]):
                        self.errors.append(
                            f"{base_error_message} {attr} is {field[attr]}."
                        )
        if type in ("string", "text"):
            valid_attributes.append("enum")
            if "enum" in field:
                if shared_enum := self.validate_enum(collectionfield, field["enum"]):
                    field["enum"] = shared_enum
            for attr in ("minLength", "maxLength"):
                valid_attributes.append(attr)
                if not isinstance(field.get(attr, 0), int):
                    self.errors.append(
                        f"'{attr}' for {collectionfield} is not a number."
                    )
            if (
                "default" in field
                and "enum" in field
                and isinstance(field["enum"], list)
                and not field["default"] in field["enum"]
            ):
                self.errors.append(
                    f"default value '{field['default']}' for {collectionfield} is not "
                    f"found in 'enum' for the field. Allowed values are: {field['enum']}."
                )

        if type in DATA_TYPES:
            valid_attributes.append("default")
            if "default" in field:
                self.validate_value_for_type(type, field["default"], collectionfield)

        if type in RELATION_TYPES:
            valid_attributes.append("on_delete")
            if "on_delete" in field and field["on_delete"] not in (
                "CASCADE",
                "PROTECT",
            ):
                self.errors.append(
                    f"invalid value for 'on_delete' for {collectionfield}"
                )
            valid_attributes.append("equal_fields")
            if type != "generic-relation-list":
                valid_attributes.append("reference")
            if nested and type in ("relation", "relation-list"):
                valid_attributes.append("enum")
            valid_attributes.append("sql")
            if field.get("sql"):
                valid_attributes.append("log_triggers")
                self.check_log_triggers(collectionfield, field)
            if "default" in field and field_name == "organization_id":
                # added as a workaround to allow defaulting to the ONE_ORGANIZATION
                print(f"Default in {collection}/{field_name} temporarily allowed.")
                valid_attributes.append("default")

        for attr in field.keys():
            if attr not in valid_attributes:
                self.errors.append(
                    f"Attribute '{attr}' for collectionfield {collectionfield} is invalid."
                )

        if not isinstance(field.get("description", ""), str):
            self.errors.append(f"Description of {collectionfield} must be a string.")

    def check_unique_together(
        self, collection: str, constraints: Any, attr_name: str
    ) -> None:
        if not isinstance(constraints, list):
            self.errors.append(
                f"Collection '{collection}': attribute {attr_name} must be a list."
            )
            return

        collection_data = self.models[collection]
        for constraint in constraints:
            field_names = [name.strip() for name in constraint.split(",")]
            if len(field_names) < 2:
                self.errors.append(
                    f"Invalid value '{constraint}' for {attr_name} constraint of '{collection}': at least 2 fields must be defined."
                )
            invalid_field_names = []
            for field_name in field_names:
                if field_name not in collection_data:
                    invalid_field_names.append(field_name)
                else:
                    if collection_data[field_name].get("unique"):
                        self.errors.append(
                            f"Field '{field_name}' can not be used in a {attr_name} constraint for collection '{collection}' because it has 'unique: true'."
                        )
            if invalid_field_names:
                self.errors.append(
                    f"Some fields from the constraint '{attr_name}' don't exist in the collection '{collection}': {', '.join(invalid_field_names)}."
                )

    def check_permissions(self) -> None:
        permissions_path = Path(PERMISSIONS_SOURCE)
        group_permissions = set(self.models["group"]["permissions"]["items"]["enum"])

        with open(permissions_path, "rb") as f:
            permissions = yaml.safe_load(f.read())
            all_permissions = {
                permission
                for collection, permissions_dict in permissions.items()
                for permission in self.flatten_permissions(collection, permissions_dict)
            }

        if group_permissions != all_permissions:
            if missing_permissions := (all_permissions - group_permissions):
                self.errors.append(
                    f"Permissions missing in group/permissions: {', '.join(list(missing_permissions))}."
                )
            if additional_permissions := (group_permissions - all_permissions):
                self.errors.append(
                    f"Permissions missing in {os.path.basename(PERMISSIONS_SOURCE)}: {', '.join(list(additional_permissions))}."
                )

    def flatten_permissions(
        self, collection: str, permissions_dict: dict[str, Any]
    ) -> set[str]:
        permissions = set()
        for key, value in permissions_dict.items():
            permissions.add(f"{collection}.{key}")
            if isinstance(value, dict):
                permissions.update(self.flatten_permissions(collection, value))
            elif value is not None:
                self.errors.append(
                    f"Permissions for '{collection}' contain invalid value: {value} (must be a dict)."
                )
        return permissions

    def validate_value_for_type(
        self, type_str: str, value: Any, collectionfield: str
    ) -> None:
        basic_types = {
            "string": str,
            "number": int,
            "boolean": bool,
            "HTMLStrict": str,
            "HTMLPermissive": str,
            "timestamp": int,
            "text": str,
        }
        if type_str in basic_types:
            if not isinstance(value, basic_types[type_str]):
                self.errors.append(
                    f"Value '{value}' for '{collectionfield}' is not a {type_str}."
                )
        elif type_str in ("string[]", "number[]", "text[]"):
            if not isinstance(value, list):
                self.errors.append(
                    f"Value '{value}' for '{collectionfield}' is not a {type_str}."
                )
            for x in value:
                if not isinstance(x, basic_types[type_str[:-2]]):
                    self.errors.append(
                        f"Listentry '{x}' for '{collectionfield}' is not a {type_str[:-2]}."
                    )
        elif type_str == "JSON":
            pass
        elif type_str == "float":
            if type(value) not in (int, float):
                self.errors.append(
                    f"Value '{value}' for '{collectionfield}' is not a float."
                )
        elif type_str == "decimal(6)":
            if not DECIMAL_REGEX.match(value):
                self.errors.append(
                    f"Value '{value}' for '{collectionfield}' is not a decimal(6)."
                )
        elif type_str == "color":
            if not COLOR_REGEX.match(value):
                self.errors.append(
                    f"Value '{value}' for '{collectionfield}' is not a color."
                )
        else:
            raise NotImplementedError(type_str)

    def check_relation(
        self, collection: str, field_name: str, field: dict[str, Any]
    ) -> str | None:
        collectionfield = f"{collection}{KEYSEPARATOR}{field_name}"
        to = field["to"]

        if isinstance(to, str):
            if not COLLECTIONFIELD_REGEX.match(to):
                return f"'to' of {collectionfield} is not a collectionfield."
            return self.check_reverse(collectionfield, field, to)
        elif isinstance(to, list):
            for cf in to:
                if not COLLECTIONFIELD_REGEX.match(cf):
                    return f"The collectionfield in 'to' of {collectionfield} is not valid."
                error = self.check_reverse(collectionfield, field, cf)
                if error:
                    return error
            self.check_reference_list(
                collectionfield, field, [c.split(KEYSEPARATOR)[0] for c in to]
            )
        else:
            to_field = to["field"]
            if not FIELD_REGEX.match(to_field):
                return (
                    f"The field '{to_field}' in 'to' of {collectionfield} is not valid."
                )
            for c in to["collections"]:
                if not COLLECTION_REGEX.match(c):
                    self.errors.append(
                        f"The collection '{c}' in 'to' of {collectionfield} is not a valid collection."
                    )
                error = self.check_reverse(
                    collectionfield, field, f"{c}{KEYSEPARATOR}{to['field']}"
                )
                if error:
                    return error
            self.check_reference_list(collectionfield, field, to["collections"])
        return None

    def setify_equal_fields(self, field_def: dict[str, Any]) -> set[str]:
        if isinstance(to_list := field_def.get("equal_fields"), list):
            return set(to_list)
        elif to_list is None:
            return set()
        else:
            return {to_list}

    def check_equal_fields(
        self,
        to_collection: str,
        to_collectionfield: str,
        to_field: dict[str, Any],
        from_collection: str,
        from_collectionfield: str,
        from_field: dict[str, Any],
    ) -> None:
        for collection_field, field in {
            to_collectionfield: to_field,
            from_collectionfield: from_field,
        }.items():
            if equal_fields := field.get("equal_fields"):
                if not (
                    isinstance(equal_fields, str)
                    or (
                        isinstance(equal_fields, list)
                        and all(isinstance(val, str) for val in equal_fields)
                    )
                ):
                    self.errors.append(
                        f"'equal_fields' of {collection_field} is not valid (must be string or list of strings)."
                    )
                    return
        joined_eq_fields = self.setify_equal_fields(to_field).union(
            self.setify_equal_fields(from_field)
        )
        if joined_eq_fields:
            for collectionfield, (collection, field, other_field) in {
                to_collectionfield: (to_collection, to_field, from_field),
                from_collectionfield: (from_collection, from_field, to_field),
            }.items():
                if (
                    not (
                        other_field["type"] == "relation"
                        or other_field["type"] == "generic-relation"
                    )
                    and collection == "user"
                    and "meeting_id" in joined_eq_fields
                ):
                    self.errors.append(
                        f"user/meeting_id handling not implemented for {collectionfield}"
                    )
                if (
                    other_field["type"] != "generic-relation"
                    and collection == "meeting"
                    and "meeting_id" in joined_eq_fields
                ):
                    self.errors.append(
                        f"meeting/meeting_id handling not implemented for {collectionfield}"
                    )
                if "sql" in field:
                    self.errors.append(
                        f"{collectionfield}: Cannot generate equal_fields triggers for sql fields"
                    )

    def check_reverse(
        self,
        from_collectionfield: str,
        from_field: dict[str, Any],
        to_collectionfield: str,
    ) -> str | None:
        to_unified = []  # a list of target collectionfields (unififed with all
        # the different possibilities for the 'to' field) from the (expected)
        # relation in to_collectionfield. The from_collectionfield must be in this
        # list.

        to_collection, to_field_name = to_collectionfield.split(KEYSEPARATOR)
        from_collection = from_collectionfield.split(KEYSEPARATOR)[0]
        if to_collection not in self.models:
            return f"The collection '{to_collection}' in 'to' of {from_collectionfield} is not a valid collection."
        if to_field_name not in self.models[to_collection]:
            return f"The collectionfield '{to_collectionfield}' in 'to' of {from_collectionfield} does not exist."

        to_field = self.models[to_collection][to_field_name]
        if to_field["type"] not in RELATION_TYPES:
            return f"{from_collectionfield} points to {to_collectionfield}, but {to_collectionfield} to is not a relation."
        self.check_equal_fields(
            from_collection,
            from_collectionfield,
            from_field,
            to_collection,
            to_collectionfield,
            to_field,
        )
        if reference_error := self.check_reference(
            from_collectionfield, from_field, to_collectionfield, to_field
        ):
            self.errors.append(reference_error)

        to = to_field["to"]
        if isinstance(to, str):
            to_unified.append(to)
        elif isinstance(to, list):
            to_unified = to
        else:
            for c in to["collections"]:
                to_unified.append(f"{c}{KEYSEPARATOR}{to['field']}")

        if from_collectionfield not in to_unified:
            return f"{from_collectionfield} points to {to_collectionfield}, but {to_collectionfield} does not point back."
        return None

    def check_log_triggers(self, collectionfield: str, field: dict[str, str]) -> None:
        if not (log_triggers := field.get("log_triggers")):
            self.errors.append(
                f"For {collectionfield} 'log_triggers' attribute must be defined because it has 'sql' attribute."
            )
            return
        elif not isinstance(log_triggers, list):
            self.errors.append(
                f"Invalid value for 'log_triggers' of {collectionfield}: must be a list of dictionaries."
            )
            return

        valid_attributes = [
            "on_table",
            "on_columns",
            "log_collection_id_column",
            "log_collection_id_sql",
            "log_value_column",
            "log_value_sql",
        ]
        for i in range(len(log_triggers)):
            base_error_message = f"Error in item {i} of {collectionfield}.log_triggers"
            log_trigger = log_triggers[i]
            if not isinstance(log_trigger, dict):
                self.errors.append(f"{base_error_message}: must be a dictionary.")
                continue
            if not (on_table := log_trigger.get("on_table")):
                self.errors.append(
                    f"{base_error_message}: missing a required attribute 'on_table'."
                )
            elif not on_table.endswith("_t"):
                self.errors.append(
                    f"{base_error_message}: '{on_table}' is not a valid value for 'on_table' (must end with '_t')."
                )

            for base_attr_name in ["log_collection_id", "log_value"]:
                values_present = [
                    bool(log_trigger.get(f"{base_attr_name}_{option}"))
                    for option in ["column", "sql"]
                ]
                if not any(values_present):
                    self.errors.append(
                        f"{base_error_message}: either '{base_attr_name}_sql' or '{base_attr_name}_column' must be defined."
                    )
                elif all(values_present):
                    print(
                        f"For for item {i} of {collectionfield}.log_triggers value in '{base_attr_name}_column' will be ignored because '{base_attr_name}_sql' is defined."
                    )

            for attr in log_trigger.keys():
                if attr not in valid_attributes:
                    self.errors.append(
                        f"{base_error_message}: attribute '{attr}' is invalid."
                    )

    def validate_enum(self, collectionfield: str, enum: Any) -> list[str] | None:
        """
        Checks that the given `enum` value is valid. If `enum` is a name of a valid
        enum defined in the _meta, returns its value.
        """
        shared_enum: list[str] | None = None
        enum_name: str | None = None

        if (
            isinstance(enum, str)
            and (shared_enum := self.shared_enum_definitions.get(enum)) is not None
        ):
            enum_name = enum
            enum = shared_enum

        if not isinstance(enum, list):
            self.errors.append(
                f"incorrect 'enum' value for {collectionfield}: '{enum}'. Must be "
                "a list of allowed values or name of the list defined in "
                f"'{os.path.basename(DEFAULT_COLLECTION_META)}'."
            )
        elif invalid_values := [
            str(item) for item in enum if not isinstance(item, str)
        ]:
            if enum_name:
                self.errors.append(
                    f"some values of 'enum' {enum_name} are not strings: {', '.join(invalid_values)}."
                )
            else:
                self.errors.append(
                    f"some values of 'enum' for {collectionfield} are not strings: {', '.join(invalid_values)}."
                )
        else:
            return shared_enum

        return None

    def split_collectionfield(self, collectionfield: str) -> tuple[str, str]:
        parts = collectionfield.split(KEYSEPARATOR)
        return parts[0], parts[1]

    def check_reference(
        self,
        from_collectionfield: str,
        from_field: dict[str, Any],
        to_collectionfield: str,
        to_field: dict[str, Any],
    ) -> str | None:
        # Check attribute presence
        reference_in_fields = ["reference" in field for field in [to_field, from_field]]
        if (
            from_field["type"] == to_field["type"]
            and from_field["type"] in ["relation", "relation-list"]
            and all(reference_in_fields)
        ):
            return f"The relation fields {from_collectionfield} and {to_collectionfield} both have 'reference' set."

        if from_field["type"] == to_field["type"] == "relation" and not any(
            reference_in_fields
        ):
            return f"One of the relation fields {from_collectionfield} and {to_collectionfield} must have 'reference' set."

        reference_allowed, reference_required = self.get_reference_allowed_required(
            from_field, to_field
        )
        reference = from_field.get("reference")
        if not reference_allowed and reference:
            return f"Relational collectionfield {from_collectionfield} can not have 'reference' attribute."
        if not reference:
            if reference_required:
                return f"Relational collectionfield {from_collectionfield} must have 'reference' attribute."
            return None

        # Check value
        if from_field["type"] in ["relation", "relation-list"]:
            if not isinstance(reference, str) or reference not in self.models:
                return f"The collection '{reference}' in 'reference' of {from_collectionfield} is not a valid collection."

            if reference != (
                to_collection := to_collectionfield.split(KEYSEPARATOR)[0]
            ):
                return f"'reference' of {from_collectionfield} must match the collection of 'to': {to_collection}."

        else:
            assert isinstance(
                reference, list
            ), f"Reference of {from_collectionfield} must be a list of valid collection names."

            if (
                to_collection := to_collectionfield.split(KEYSEPARATOR)[0]
            ) not in reference:
                return f"'reference' of {from_collectionfield} is missing a collection '{to_collection}' from 'to'."
        return None

    def check_reference_list(
        self,
        from_collectionfield: str,
        from_field: dict[str, Any],
        to_collections: list[Any],
    ) -> None:
        if not (reference := from_field.get("reference")):
            return

        if invalid := [value for value in reference if value not in self.models]:
            self.errors.append(
                f"'reference' of {from_collectionfield} contains values that are not valid collections: {invalid}"
            )

        if not_in_to := [
            value
            for value in reference
            if value not in to_collections and value not in invalid
        ]:
            self.errors.append(
                f"'reference' of {from_collectionfield} contains collections not present in 'to': {not_in_to}"
            )

    def get_reference_allowed_required(
        self, from_field: dict[str, Any], to_field: dict[str, Any]
    ) -> tuple[bool, bool]:
        if from_field["type"] == to_field["type"] == "relation":
            return (True, False)
        if from_field["type"] == "relation-list" and to_field["type"] in [
            "relation",
            "generic-relation",
        ]:
            return (True, False)
        if (from_field["type"], to_field["type"]) in (
            ("generic-relation", "relation-list"),
            ("generic-relation", "relation"),
            ("relation", "relation-list"),
        ):
            return (True, True)
        return (False, False)


def main() -> int:
    dirs = sys.argv[1:]
    if not dirs:
        dirs = [DEFAULT_COLLECTIONS_DIR]

    failed = False
    for d in dirs:
        try:
            Checker(d).run_check()
        except CheckException as e:
            print(f"Check for {d} failed:\n", e)
            failed = True
        else:
            print(f"Check for {d} successful.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
