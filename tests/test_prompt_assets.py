from pathlib import Path
import json

import pytest

from FAGENT.engine.services import GraphServices
from FAGENT.engine.workflow import load_workflow
from test_task_config import task


def test_assets_reload(task):
    workflow = load_workflow(task)
    prompt = task.parent.parent / workflow.prompts[0]
    tools = task.parent.parent / workflow.tools
    prompt.write_text('Alternative system prompt')
    specs = json.loads(tools.read_text())
    specs[0]['function']['description'] = 'Alternative tool description'
    tools.write_text(json.dumps(specs))
    refreshed = load_workflow(task)
    services = GraphServices(Path('.'), {'rag_context_chars': 200}, refreshed)
    text = services.system_prompt([{'content': 'reference' * 100}])
    assert 'Alternative system prompt' in text
    assert 'Alternative tool description' in text
    assert 'Retrieved reference data' in text
    assert refreshed.digest != workflow.digest


def test_invalid_tool_schema(task):
    workflow = load_workflow(task)
    tools = task.parent.parent / workflow.tools
    specs = json.loads(tools.read_text())
    specs[0]['function']['parameters']['type'] = 'invalid'
    tools.write_text(json.dumps(specs))
    with pytest.raises(ValueError):
        load_workflow(task)
