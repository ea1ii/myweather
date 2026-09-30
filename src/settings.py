#!/usr/bin/env python3

import argparse
import json
from pathlib import Path

DEFAULT_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.json"


def load_config(path):
    with path.open(encoding="utf-8") as config_file:
        config = json.load(config_file)
    if not isinstance(config, dict):
        raise ValueError("The settings file must contain a JSON object")
    return config


def iter_leaves(value, prefix=""):
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{prefix}.{key}" if prefix else key
            yield from iter_leaves(child, child_path)
    else:
        yield prefix, value


def resolve_parent(config, dotted_path):
    parts = dotted_path.split(".")
    if not all(parts):
        raise ValueError("Parameter paths must use non-empty dot-separated names")

    parent = config
    for part in parts[:-1]:
        if not isinstance(parent, dict) or part not in parent:
            raise ValueError(f"Unknown parameter path: {dotted_path}")
        parent = parent[part]
    if not isinstance(parent, dict) or parts[-1] not in parent:
        raise ValueError(f"Unknown parameter path: {dotted_path}")
    if isinstance(parent[parts[-1]], dict):
        raise ValueError(f"Choose a leaf parameter, not a group: {dotted_path}")
    return parent, parts[-1]


def parse_value(raw_value):
    try:
        return json.loads(raw_value)
    except json.JSONDecodeError:
        return raw_value


def validate_value(config, dotted_path, current, new_value):
    is_number = lambda value: isinstance(value, (int, float)) and not isinstance(value, bool)
    if not (type(current) is type(new_value) or (is_number(current) and is_number(new_value))):
        raise ValueError(f"Value must have the same type as the current value ({type(current).__name__})")

    if dotted_path.endswith(".polynomial.degree"):
        if type(new_value) is not int or not 0 <= new_value <= 4:
            raise ValueError("Polynomial degree must be an integer from 0 to 4")


def main():
    parser = argparse.ArgumentParser(description="View or update weather settings in the JSON file.")
    parser.add_argument("--config", type=Path, default=DEFAULT_SETTINGS_PATH,
                        help="Settings file path (default: %(default)s)")
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--list", action="store_true", help="List all setting paths and values")
    operation.add_argument("--get", metavar="PATH", help="Show one setting value")
    operation.add_argument("--set", nargs=2, metavar=("PATH", "VALUE"),
                           help="Set a setting; VALUE may be JSON or a plain string")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
        if args.list:
            for dotted_path, value in iter_leaves(config):
                print(f"{dotted_path} = {json.dumps(value)}")
            return

        if args.get:
            parent, key = resolve_parent(config, args.get)
            print(json.dumps(parent[key]))
            return

        dotted_path, raw_value = args.set
        parent, key = resolve_parent(config, dotted_path)
        current = parent[key]
        new_value = parse_value(raw_value)
        validate_value(config, dotted_path, current, new_value)
        parent[key] = new_value
        args.config.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        print(f"Updated {dotted_path}: {json.dumps(current)} -> {json.dumps(new_value)}")
    except (OSError, json.JSONDecodeError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()