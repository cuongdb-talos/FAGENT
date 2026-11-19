"""Unified CLI application class for FAGENT driven by setup YAML and config JSON."""

# ==============================================================================
# SECTION 1: Standard and Third-Party Library Imports
# ==============================================================================
import argparse
import json
import re
from pathlib import Path
import sys

import yaml

# ==============================================================================
# SECTION 2: Internal FAGENT Module Imports
# ==============================================================================
from .engine.graph_runner import GraphRunner
from .engine.graph_services import GraphServices
from .engine.workflow_loader import WorkflowLoader


# ==============================================================================
# SECTION 3: Main Application Controller Class
# ==============================================================================
class Fagent:
    """Main CLI orchestrator responsible for lifecycle, setup loading, and workflow execution."""

    # --------------------------------------------------------------------------
    # Constants and Default Configuration Paths
    # --------------------------------------------------------------------------
    PACKAGE_DIR = Path(__file__).resolve().parent
    DEFAULT_CONFIG = PACKAGE_DIR / 'config.json'
    DEFAULT_SETUP = 'env_setup.yaml'
    TASK_ROOT = PACKAGE_DIR / 'tasks'
    ARTIFACT_FILES = ('graph-checkpoints.sqlite3', 'conversation.json', 'result.json')
    PROMPT_MIGRATION = {
        'knowledge/prompts/drc_site.md': 'knowledge/templates/prompts/drc_site.md',
        'knowledge/prompts/drc.md': 'tasks/drc/prompts/drc.md',
        'knowledge/prompts/lvs.md': 'tasks/lvs/prompts/lvs.md',
        'knowledge/prompts/protocol.md': 'knowledge/templates/prompts/protocol.md',
        'knowledge/prompts/review.md': 'knowledge/templates/prompts/review.md',
    }

    # --------------------------------------------------------------------------
    # Main Execution Flow (Primary Entry Point)
    # --------------------------------------------------------------------------
    def run(self) -> int:
        self.args = self.parse_args()
        try:
            self.config, self.config_path = self.load_config(self.args.config)
            self.setup, self.setup_path = self.load_setup(self.args.setup)
            self.result = self.run_setup(self.setup, self.setup_path, self.config)
        except (ValueError, OSError, TypeError, KeyError, yaml.YAMLError) as exc:
            self.fail(str(exc))
        return 0

    # --------------------------------------------------------------------------
    # Initialization and Class Constructors
    # --------------------------------------------------------------------------
    def __init__(self, argv: list[str] | None = None):
        self.argv = argv
        self.args = None
        self.config = {}
        self.config_path = None
        self.setup = {}
        self.setup_path = None
        self.result = []

    @classmethod
    def main(cls, argv: list[str] | None = None) -> int:
        return cls(argv).run()

    # --------------------------------------------------------------------------
    # Helper & Validation Utilities
    # --------------------------------------------------------------------------
    def fail(self, message):
        print(f'[ERROR] {message.removeprefix("[ERROR] ")}', file=sys.stderr)
        sys.exit(1)

    def normalize_goal(self, raw_prompts):
        if isinstance(raw_prompts, str):
            return raw_prompts.strip()
        if isinstance(raw_prompts, list):
            return '\n'.join(str(item).strip() for item in raw_prompts if str(item).strip())
        return ''

    def parse_args(self) -> argparse.Namespace:
        parser = argparse.ArgumentParser(description='Local DRC and LVS agent')
        parser.add_argument('--setup', default=self.DEFAULT_SETUP, help='YAML setup file')
        parser.add_argument('--config', default=str(self.DEFAULT_CONFIG), help='Environment config JSON file')
        parser.add_argument('--verbose', action='store_true', help='Show live workflow execution')
        return parser.parse_args(self.argv)

    # --------------------------------------------------------------------------
    # Configuration and Setup Loaders
    # --------------------------------------------------------------------------
    def load_config(self, raw_path):
        config_path = Path(raw_path).resolve()
        if not config_path.is_file():
            self.fail(f'Missing config file: {config_path}')
        config = json.loads(config_path.read_text())
        prompt_file = config.get('system_prompt_file')
        if prompt_file:
            mapped = self.PROMPT_MIGRATION.get(prompt_file, prompt_file)
            config['system_prompt_file'] = str((config_path.parent / mapped).resolve())
        return config, config_path

    def load_setup(self, raw_path):
        setup_path = Path(raw_path).resolve()
        if not setup_path.is_file():
            self.fail(f'Missing setup file: {setup_path}')
        setup = yaml.safe_load(setup_path.read_text())
        if not isinstance(setup, dict):
            self.fail('setup must be a YAML mapping')
        return setup, setup_path

    def load_task_workflow(self, task_name, task_cfg, base):
        """Resolve a selected task without a central task list."""
        if not isinstance(task_name, str) or not re.fullmatch(r'[a-z][a-z0-9_]*', task_name):
            raise ValueError(f'Invalid task name: {task_name}')
        path = self.TASK_ROOT / task_name / 'workflow.yaml'
        if custom := task_cfg.get('workflow'):
            if (base / custom).resolve() != path:
                raise ValueError(f'{task_name}: workflow must reside in its task package')
        return WorkflowLoader(path, self.TASK_ROOT).run()

    # --------------------------------------------------------------------------
    # Task and Graph Execution Dispatchers
    # --------------------------------------------------------------------------
    def run_setup(self, setup, setup_path, config):
        base = setup_path.parent
        output_folder = base / setup['output_folder']
        tasks = setup.get('tasks')
        if not isinstance(tasks, list) or not tasks:
            self.fail('tasks must be a non-empty list in setup YAML')

        results = []
        for entry in tasks:
            if not isinstance(entry, dict) or len(entry) != 1:
                self.fail('Each task entry in setup YAML must map one task name to its config')
            task_name, task_cfg = next(iter(entry.items()))
            if not isinstance(task_cfg, dict):
                self.fail(f'Task config for {task_name} must be a mapping')
            workflow = self.load_task_workflow(task_name, task_cfg, base)
            task_config = dict(config)
            goal = self.normalize_goal(task_cfg.get('prompts') if 'prompts' in task_cfg else task_cfg.get('prompt'))
            if goal:
                task_config['goal'] = goal

            rundir = output_folder / task_name / 'rundir'
            if workflow.prepare:
                runs = workflow.prepare(task_cfg, setup, base, output_folder, task_config, False)
            else:
                runs = ((rundir, task_config, task_cfg),)
            for rundir, run_config, inputs in runs:
                for name in self.ARTIFACT_FILES:
                    artifact = rundir / name
                    if artifact.exists() or artifact.is_symlink():
                        self.fail(f'Output already exists: {artifact}')
                result = self.run_graph(rundir, workflow, run_config, inputs)
                print(json.dumps(result, ensure_ascii=False))
                results.append(result)
        return results

    def run_graph(self, rundir, workflow, config, inputs):
        """Prepare dependencies and run the selected workflow."""
        services = GraphServices(rundir, config, workflow, verbose=self.args.verbose)
        messages = services.prepare(resume=False)
        runner = GraphRunner(services, rundir)
        return runner.run(messages, inputs)
