from __future__ import annotations

import pytest
from pydantic import BaseModel

from insia_agents.models import Draft, Plan, ResearchPack, Review
from insia_agents.schema import UNSUPPORTED_KEYS, SchemaError, iter_objects, json_format, output_schema


def _walk(node):
    if isinstance(node, dict):
        yield node
        for key, value in node.items():
            if key == "properties":
                for sub in value.values():
                    yield from _walk(sub)
            else:
                yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


@pytest.mark.parametrize("model", [Plan, ResearchPack, Draft, Review])
def test_strict_schema(model):
    schema = output_schema(model)
    objects = list(iter_objects(schema))
    assert objects, "expected object schemas"
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])
    for node in _walk(schema):
        assert "$ref" not in node and "$defs" not in node
        assert not (set(node) & UNSUPPORTED_KEYS), set(node) & UNSUPPORTED_KEYS
        # no dict-typed (free-form) properties anywhere
        assert node.get("additionalProperties", False) is False
    assert json_format(model) == {"type": "json_schema", "schema": schema}


def test_all_fields_required_even_with_defaults():
    schema = output_schema(Draft)
    assert set(schema["required"]) == {"channel", "round", "title", "content", "hashtags", "used_finding_ids", "change_log"}
    source = output_schema(ResearchPack)["properties"]["sources"]["items"]
    assert source["properties"]["tier"]["enum"] == [1, 2, 3]
    assert "title" in source["properties"]  # a property *named* title survives


def test_dict_fields_are_rejected():
    class Bad(BaseModel):
        scores: dict[str, int]

    with pytest.raises(SchemaError):
        output_schema(Bad)


def test_recursive_models_are_rejected():
    class Node(BaseModel):
        name: str
        children: list["Node"] = []

    Node.model_rebuild()
    with pytest.raises(SchemaError):
        output_schema(Node)
