"""Load task definitions and bind their validated assets to a digest."""
from hashlib import sha256
import json
from pathlib import Path
from typing import Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field
import yaml

from ..capabilities.registry import ACTIONS


class Step(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)

    id: str = Field(pattern=r'^[a-z][a-z0-9_]*$')
    action: str
    requires: tuple[str, ...] = ()


class WorkflowDefinition(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)

    task: Literal['drc', 'lvs']
    version: int = Field(gt=0)
    prompts: tuple[str, ...] = Field(min_length=1)
    protocol: str
    schema_path: str = Field(alias="schema")
    review_prompt: str
    review_schema: str
    review_text: str = ""
    feedback_schema: dict = Field(default_factory=dict)
    tools: str | None = None
    inputs: tuple[str, ...]
    backend: Literal['local', 'real']
    mock_pass: bool = False
    steps: tuple[Step, ...] = Field(min_length=1)
    prompt_text: tuple[str, ...] = ()
    protocol_text: str = ''
    response_schema: dict = Field(default_factory=dict)
    tool_specs: tuple[dict, ...] = ()
    reference_label: str = 'Retrieved reference data'
    digest: str = ''


def load_workflow(path: Path) -> WorkflowDefinition:
    path = Path(path).resolve()
    root = path.parent.parent
    try:
        raw = path.read_text()
        data = yaml.safe_load(raw)
        fields = {'task', 'version', 'prompts', 'protocol', 'schema', 'review_prompt', 'review_schema', 'tools', 'inputs', 'backend', 'mock_pass', 'steps'}
        if not isinstance(data, dict) or data.keys() - fields:
            raise ValueError('Unknown workflow fields')
        workflow = WorkflowDefinition.model_validate(data)
        seen = set()
        actions = []
        for step in workflow.steps:
            if step.id == 'repair_tools' or step.id in seen or step.action not in ACTIONS or ACTIONS[step.action] not in (None, workflow.task):
                raise ValueError(f'Duplicate step or unregistered action: {step.id}/{step.action}')
            if set(step.requires) - seen:
                raise ValueError(f'Unresolved prerequisites for {step.id}')
            seen.add(step.id)
            actions.append(step.action)
        expected = {'drc': ['read_drc', 'repair_drc', 'validate_drc', 'review'],
                    'lvs': ['read_lvs', 'repair_lvs', 'validate_lvs', 'review']}
        if actions != expected[workflow.task]:
            raise ValueError(f'Invalid action order for {workflow.task}')
        prompts = tuple(read_asset(root, name, 'knowledge/prompts') for name in workflow.prompts)
        protocol = read_asset(root, workflow.protocol, 'knowledge/prompts')
        schema_text = read_asset(root, workflow.schema_path, 'workflows/schemas')
        schema = json.loads(schema_text)
        if not isinstance(schema, dict) or schema.get('type') != 'object':
            raise ValueError('A response object schema is required')
        check_schema(schema)
        tools_text = read_asset(root, workflow.tools, 'workflows/tools') if workflow.tools else '[]'
        tools = json.loads(tools_text)
        if not isinstance(tools, list):
            raise ValueError('Tool definitions must be a list')
        names = set()
        for tool in tools:
            if not isinstance(tool, dict) or tool.get('type') != 'function' or not isinstance(tool.get('function'), dict):
                raise ValueError('Invalid tool definition')
            function = tool['function']
            name = function.get('name')
            if workflow.task != 'drc' or name not in ('read_gds', 'replace_polygon') or name in names:
                raise ValueError(f'Invalid or duplicate tool: {name}')
            names.add(name)
            parameters = function.get('parameters')
            if not isinstance(parameters, dict) or parameters.get('type') != 'object' or not isinstance(function.get('description'), str) or not function['description'].strip():
                raise ValueError(f'Invalid tool schema or description: {name}')
            check_schema(parameters)
        if workflow.task == 'drc' and names != {'read_gds', 'replace_polygon'}:
            raise ValueError('DRC requires geometry inspection and repair tools')
        review_text = read_asset(root, workflow.review_prompt, 'knowledge/prompts')
        review_schema_text = read_asset(root, workflow.review_schema, 'workflows/schemas')
        feedback_schema = json.loads(review_schema_text)
        check_schema(feedback_schema)
        if feedback_schema.get('type') != 'object':
            raise ValueError('A review response object schema is required')
        digest = sha256(json.dumps([raw, prompts, protocol, schema_text, tools_text, review_text, review_schema_text]).encode()).hexdigest()
        return workflow.model_copy(update={'prompt_text': prompts, 'protocol_text': protocol,
                                           'response_schema': schema, 'review_text': review_text, 'feedback_schema': feedback_schema, 'tool_specs': tuple(tools), 'digest': digest})
    except (OSError, ValueError, yaml.YAMLError, SchemaError) as exc:
        raise ValueError(f'Invalid workflow {path}: {exc}') from exc


def read_asset(root, name, directory):
    allowed = (root / directory).resolve()
    target = (root / name).resolve()
    if Path(name).is_absolute() or not allowed.is_relative_to(root) or not target.is_relative_to(allowed):
        raise ValueError(f'Asset must be inside {directory}: {name}')
    value = target.read_text()
    if not value.strip():
        raise ValueError(f'Empty asset: {name}')
    return value


def check_schema(schema):
    Draft202012Validator.check_schema(schema)
    pending = [schema]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ('$ref', '$dynamicRef', '$recursiveRef') and (
                    not isinstance(item, str) or not item.startswith('#')
                ):
                    raise ValueError('External schema reference is forbidden')
                pending.append(item)
        elif isinstance(value, list):
            pending.extend(value)
