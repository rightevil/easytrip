"""Validate server schemas without fetching references outside the schema."""

from jsonschema.validators import validator_for
from referencing import Registry
from referencing.exceptions import Unresolvable


def validate_arguments(arguments, schema):
    pending = [schema]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for key, item in value.items():
                if (
                    key in {"$ref", "$dynamicRef", "$recursiveRef"}
                    and isinstance(item, str)
                    and not item.startswith("#")
                ):
                    raise ValueError("MCP 工具参数定义包含不可解析的引用")
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    validator = validator_for(schema)
    validator.check_schema(schema)
    try:
        validator(schema, registry=Registry()).validate(arguments)
    except (Unresolvable, RecursionError):
        raise ValueError("MCP 工具参数定义包含不可解析的引用") from None
