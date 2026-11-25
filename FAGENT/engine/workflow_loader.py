"""Load task definitions and bind their validated assets to a digest."""

# ==============================================================================
# SECTION 1: Standard and Third-Party Library Imports
# ==============================================================================
from hashlib import sha256
import json
import inspect
import re
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import jsonschema
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
import yaml

# ==============================================================================
# SECTION 2: Internal Module Imports
# ==============================================================================
from .task_contracts import ToolSpec


# ==============================================================================
# SECTION 3: Workflow Definition and Asset Loader Class
# ==============================================================================
class WorkflowLoader:
    """Loads, validates, and hashes declarative YAML workflow specs and referenced schemas/prompts."""

    # --------------------------------------------------------------------------
    # Schema File Constants
    # --------------------------------------------------------------------------
    WORKFLOW_SCHEMA = 'schemas/workflow.json'

    # --------------------------------------------------------------------------
    # Main Workflow Execution Entry Point
    # --------------------------------------------------------------------------
    def run(self) -> SimpleNamespace:
        """Load a task workflow and retain the validated definition."""
        try:
            self.workflow = self._load_task()
            return self.workflow
        except (OSError, ValueError, yaml.YAMLError, SchemaError, jsonschema.ValidationError) as exc:
            raise ValueError(f'[ERROR] Invalid workflow {self.path}: {exc}') from exc

    # --------------------------------------------------------------------------
    # Initialization and Path Resolution
    # --------------------------------------------------------------------------
    def __init__(self, path: Path, task_root: Path | None = None):
        """Configure the workflow path and allowed task root."""
        self.path = Path(path).absolute()
        self.task_root = (Path(task_root) if task_root is not None else Path(__file__).resolve().parents[1] / "tasks").resolve()
        self.templates_root = Path(__file__).resolve().parents[1] / 'knowledge/templates'
        self.workflow: SimpleNamespace | None = None

    # === Task-local workflow resolution ===
    def _load_task(self):
        """Validate paths and routes before importing any referenced module."""
        directory = self.path.parent
        task = directory.name
        if (
            not re.fullmatch(r'[a-z][a-z0-9_]*', task)
            or directory.parent != self.task_root
            or directory.resolve() != directory
            or self.path.name != 'workflow.yaml'
        ):
            raise ValueError(f'Invalid task directory or workflow path: {task}')
        sources = {}
        raw = self._read_local(directory, self.path.name, sources)
        data = yaml.safe_load(raw)
        definition = self.templates_root / self.WORKFLOW_SCHEMA
        Draft202012Validator(json.loads(definition.read_text())).validate(data)
        if data['task'] != task:
            raise ValueError(f'Task name must match directory: {task}')
        steps = data.get('nodes') or data.get('steps')
        if not steps:
            raise ValueError('Workflow must declare nodes')
        ids = {step['id'] for step in steps}
        if len(ids) != len(steps):
            raise ValueError('Duplicate step IDs')
        references = {}
        prepare_reference = data.get('prepare')
        entries = list(steps)
        if prepare_reference:
            if prepare_reference in {'agent', 'review'}:
                raise ValueError('Prepare must reference a task callable')
            entries.append({'id': 'prepare', 'action': prepare_reference, 'next': 'END'})
        for step in entries:
            action = step['action']
            identifier = step['id']
            if step['next'] not in ids | {'END'}:
                raise ValueError(f'{identifier}: unknown route {step["next"]}')
            if action == 'review' and step.get('continue') not in ids:
                raise ValueError(f'{identifier}: invalid continue route {step.get("continue")}')
            if action != 'review' and 'continue' in step:
                raise ValueError(f'{identifier}: only review can declare a continue route')
            if action != 'agent' and 'tools' in step:
                raise ValueError(f'{identifier}: only agent can declare tools')
            bindings = step.get('tools', []) if action == 'agent' else []
            if isinstance(action, list):
                bindings = list(action)
            elif action not in {'agent', 'plan', 'review'}:
                bindings = [action]
            for reference in bindings:
                match = re.fullmatch(r'([a-zA-Z_][a-zA-Z0-9_]*):([a-zA-Z_][a-zA-Z0-9_]*)', reference)
                if not match:
                    raise ValueError(f'{identifier}: invalid callable reference {reference}')
                filename = match[1] + '.py'
                try:
                    self._read_local(directory, filename, sources)
                except (OSError, ValueError) as exc:
                    raise ValueError(f'{identifier}: {reference}: {exc}') from exc
                references[reference] = filename, match[2]

        prompts = tuple(self._read_local(directory, name, sources).decode() for name in data.get('prompts', ()))
        response_schema = None
        if 'schema' in data:
            schema_text = self._read_local(directory, data['schema'], sources).decode()
            response_schema = self._parse_object_schema(schema_text, 'A response object schema is required')
        agent_steps = [s for s in steps if s.get('action') == 'agent']
        has_prompts = bool(prompts) or all(
            s.get('prompt') or (directory / 'prompts' / f'{s["id"]}.md').is_file()
            for s in agent_steps
        )
        if agent_steps and (not has_prompts or response_schema is None):
            raise ValueError('Agent steps require prompts and a response schema')

        shared = {}
        for key, folder in (('protocol', 'prompts'), ('review_prompt', 'prompts'), ('review_schema', 'schemas')):
            if key in data:
                path = Path(data[key])
                if path.parent.as_posix() != f'knowledge/templates/{folder}':
                    raise ValueError(f'Invalid shared asset: {path}')
                shared[key] = self._read_local(self.templates_root, f'{folder}/{path.name}', sources, prefix='shared/').decode()
        if any(step['action'] == 'review' for step in steps):
            if ('review_prompt' in shared) != ('review_schema' in shared):
                raise ValueError('Review requires both prompt and schema when configured')
        feedback_schema = self._parse_object_schema(shared['review_schema'], 'A review object schema is required') if 'review_schema' in shared else None
        modules = {}
        prepare = None
        if prepare_reference:
            try:
                prepare = self._bind(directory, prepare_reference, references, sources, modules)
                inspect.signature(prepare).bind(*[object()] * 6)
            except (ValueError, TypeError, ImportError, SyntaxError) as exc:
                raise ValueError(f'prepare: {prepare_reference}: {exc}') from exc
        resolved = []
        for step in steps:
            try:
                function = None
                tools = {}
                step_schema = None
                step_prompt = None
                if 'schema' in step:
                    schema_text = self._read_local(directory, step['schema'], sources).decode()
                    step_schema = self._parse_object_schema(schema_text, f'{step["id"]}: step schema must be an object schema')
                if 'prompt' in step:
                    step_prompt = self._read_local(directory, step['prompt'], sources).decode()
                elif (directory / 'prompts' / f'{step["id"]}.md').is_file():
                    step_prompt = self._read_local(directory, f'prompts/{step["id"]}.md', sources).decode()
                if step['action'] == 'plan' and (step_schema is None or not step_prompt):
                    raise ValueError('Plan step requires step-local prompt and schema')
                if isinstance(step['action'], list):
                    bound_actions = []
                    for reference in step['action']:
                        bound = self._bind(directory, reference, references, sources, modules)
                        self._check_signature(bound)
                        bound_actions.append(bound)
                    functions = tuple(bound_actions)
                elif step['action'] not in {'agent', 'plan', 'review'}:
                    function = self._bind(directory, step['action'], references, sources, modules)
                    self._check_signature(function)
                    functions = (function,)
                else:
                    functions = ()
                for reference in step.get('tools', ()):
                    target = self._bind(directory, reference, references, sources, modules)
                    name = references[reference][1]
                    if name in tools:
                        raise ValueError(f'Duplicate tool: {name}')
                    metadata = getattr(target, 'task_tool', None)
                    if not isinstance(metadata, ToolSpec) or not isinstance(metadata.description, str) or not metadata.description.strip():
                        raise ValueError(f'Invalid tool metadata: {reference}')
                    try:
                        self._parse_object_schema(json.dumps(metadata.parameters), 'Tool parameters must be an object schema')
                        self._check_signature(target, metadata.parameters)
                    except (ValueError, TypeError, SchemaError) as exc:
                        raise ValueError(f'{reference}: {exc}') from exc
                    tools[name] = metadata
                resolved.append(SimpleNamespace(**{
                    **step,
                    'continue_to': step.get('continue'),
                    'function': function,
                    'functions': functions,
                    'tools': tools,
                    'schema': step_schema,
                    'prompt': step_prompt,
                }))
            except (ValueError, TypeError, ImportError, SyntaxError) as exc:
                raise ValueError(f'{step["id"]}: {exc}') from exc
        digest = sha256()
        for name, content in sorted(sources.items()):
            digest.update(json.dumps([name, sha256(content).hexdigest()]).encode())
        return SimpleNamespace(**{
            **data, 'nodes': tuple(resolved), 'steps': tuple(resolved), 'prompt_text': prompts,
            'response_schema': response_schema, 'digest': digest.hexdigest(),
            'prepare': prepare, 'protocol_text': shared.get('protocol', ''),
            'review_text': shared.get('review_prompt'),
            'feedback_schema': feedback_schema, 'mock_pass': data.get('mock_pass', False),
            'reference_label': 'Retrieved reference data',
            'layout_services': data.get('layout_services', False),
            'knowledge_review': data.get('knowledge_review', False),
        })

    def _read_local(self, root, name, sources, prefix=''):
        """Read and record a non-empty asset contained in the task directory."""
        relative = Path(name)
        path = (root / relative).resolve()
        if relative.is_absolute() or '..' in relative.parts or not path.is_relative_to(root):
            raise ValueError(f'Path escapes task directory: {name}')
        key = (prefix, name)
        if key not in sources:
            sources[key] = path.read_bytes()
        if not sources[key].strip():
            raise ValueError(f'Empty task asset: {name}')
        return sources[key]

    def _bind(self, directory, reference, references, sources, modules):
        """Resolve a callable from the exact bytes included in the digest."""
        filename, name = references[reference]
        if filename not in modules:
            path = directory / filename
            spec = spec_from_file_location(f'_{sha256(str(path).encode()).hexdigest()}', path)
            module = module_from_spec(spec)
            exec(compile(sources['', filename], str(path), 'exec'), module.__dict__)
            modules[filename] = module
        function = getattr(modules[filename], name, None)
        if not callable(function):
            raise ValueError(f'Missing callable: {reference}')
        return function

    def _check_signature(self, function, schema=None):
        """Ensure graph calls match the callable's declared arguments."""
        signature = inspect.signature(function)
        if schema is None:
            try:
                signature.bind(object())
            except TypeError as exc:
                raise ValueError(f'Invalid step signature: {signature}') from exc
            return
        required = set(schema.get('required', ()))
        properties = set(schema.get('properties', ())) | required
        try:
            signature.bind(object(), **dict.fromkeys(required))
            signature.bind(object(), **dict.fromkeys(properties))
        except TypeError as exc:
            raise ValueError(f'Invalid tool signature: {signature}') from exc
        if schema.get('additionalProperties') is not False and not any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()
        ):
            raise ValueError(f'Tool signature must accept additional arguments: {signature}')

    def _parse_object_schema(self, text: str, error_msg: str) -> dict:
        schema = json.loads(text)
        if not isinstance(schema, dict) or schema.get('type') != 'object':
            raise ValueError(error_msg)
        self.check_schema(schema)
        return schema

    def check_schema(self, schema: dict) -> None:
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
