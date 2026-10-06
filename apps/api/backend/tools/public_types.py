"""Print TypeScript from the actual public Pydantic schema; --check detects drift."""
import argparse
import json
from pathlib import Path

from backend.services.public_projection import JobResponse, JobEnvelope, JobsResponse, AcceptedJobResponse


def ts_type(schema):
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    if "enum" in schema:
        return " | ".join(json.dumps(value, ensure_ascii=False) for value in schema["enum"])
    if "anyOf" in schema:
        return " | ".join(ts_type(item) for item in schema["anyOf"])
    kind = schema.get("type")
    if kind == "array":
        return f"Array<{ts_type(schema['items'])}>"
    if kind == "object":
        return "Record<string, unknown>"
    return {"string": "string", "integer": "number", "number": "number", "boolean": "boolean", "null": "null"}.get(kind, "unknown")


def render():
    definitions = {}
    for model in (JobResponse, JobEnvelope, JobsResponse, AcceptedJobResponse):
        schema = model.model_json_schema(mode="serialization")
        definitions.update(schema.get("$defs", {}))
        if "properties" in schema:
            definitions[model.__name__] = schema
    lines = ["// Generated from backend.services.public_projection; do not edit by hand.", ""]
    for name, definition in sorted(definitions.items()):
        lines.append(f"export interface {name} {{")
        # Serialized model_dump includes defaults, so every response field is present.
        for key, field in definition["properties"].items():
            lines.append(f"  {key}: {ts_type(field)};")
        lines.extend(["}", ""])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", type=Path)
    args = parser.parse_args()
    source = render()
    if args.check:
        if args.check.read_text() != source:
            raise SystemExit("Public API TypeScript types are stale; regenerate from Pydantic")
    else:
        print(source, end="")


if __name__ == "__main__":
    main()
