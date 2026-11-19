"""Verify task-local discovery, validation, and source identity."""

# === SECTION 1: Imports ===
from pathlib import Path
from shutil import copytree

import pytest

from FAGENT.engine.workflow_loader import WorkflowLoader


# === SECTION 2: Loader tests ===
class TestTaskLoader:
    """Exercise a copied fixture so mutations remain isolated."""

    def run(self, tmp_path):
        """Copy the fixture and return its workflow path."""
        fixture = Path(__file__).parent / "fixtures/tasks/migrate_gds"
        return copytree(fixture, tmp_path / fixture.name) / "workflow.yaml"

    @classmethod
    def load(cls, path):
        """Resolve a workflow within the fixture root."""
        return WorkflowLoader(path, task_root=path.parent.parent).run()

    @classmethod
    def replace(cls, path, old, new):
        """Change one workflow fragment."""
        path.write_text(path.read_text().replace(old, new))

    @classmethod
    def test_loads_task_local_step_and_tool(cls, tmp_path):
        """Resolve step and model tool callables."""
        workflow = cls.load(cls().run(tmp_path))
        assert callable(workflow.steps[0].function)
        assert workflow.steps[1].tools["inspect_layer"].parameters["required"] == ["layer"]

    @classmethod
    @pytest.mark.parametrize("reference", ["../external:act", "/external:act"])
    def test_invalid_task_or_module_path_rejected_before_import(cls, tmp_path, reference):
        """Reject invalid module references before executing task code."""
        path = cls().run(tmp_path)
        marker = tmp_path / "marker"
        (path.parent / "steps.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
        cls.replace(path, "steps:validate_gds", reference)
        with pytest.raises(ValueError, match="validate"):
            cls.load(path)
        assert not marker.exists()

    @classmethod
    def test_symlink_escape_rejected(cls, tmp_path):
        """Reject a module symlink leading outside the task."""
        path = cls().run(tmp_path)
        external = tmp_path / "external.py"
        external.write_text("raise AssertionError('imported')\n")
        (path.parent / "escape.py").symlink_to(external)
        cls.replace(path, "steps:read_gds", "escape:act")
        with pytest.raises(ValueError, match="read"):
            cls.load(path)

    @classmethod
    @pytest.mark.parametrize(("old", "new"), [
        ("id: validate", "id: read"),
        ("['tools:inspect_layer']", "['tools:inspect_layer', 'tools:inspect_layer']"),
    ])
    def test_duplicate_ids_and_tool_names_rejected(cls, tmp_path, old, new):
        """Reject ambiguous steps and model tool names."""
        path = cls().run(tmp_path)
        cls.replace(path, old, new)
        with pytest.raises(ValueError, match="Duplicate"):
            cls.load(path)

    @classmethod
    def test_missing_function_and_bad_tool_schema_rejected(cls, tmp_path):
        """Identify a missing callable and invalid JSON Schema."""
        path = cls().run(tmp_path)
        cls.replace(path, "steps:read_gds", "steps:missing")
        with pytest.raises(ValueError, match="missing"):
            cls.load(path)
        cls.replace(path, "steps:missing", "steps:read_gds")
        cls.replace(path.parent / "tools.py", '"integer"', '"unknown"')
        with pytest.raises(ValueError, match="inspect_layer"):
            cls.load(path)

    @classmethod
    def test_invalid_route_and_signature_rejected(cls, tmp_path):
        """Reject unresolved routes and incompatible step arguments."""
        path = cls().run(tmp_path)
        cls.replace(path, "next: migrate", "next: missing")
        with pytest.raises(ValueError, match="missing"):
            cls.load(path)
        cls.replace(path, "next: missing", "next: migrate")
        cls.replace(path.parent / "steps.py", "run(context)", "run(context, extra)")
        with pytest.raises(ValueError, match="signature"):
            cls.load(path)

    @classmethod
    def test_digest_changes_when_callable_source_changes(cls, tmp_path):
        """Bind edited source bytes to checkpoint identity."""
        path = cls().run(tmp_path)
        digest = cls.load(path).digest
        with (path.parent / "steps.py").open("a") as stream:
            stream.write("\n# Changed source.\n")
        assert cls.load(path).digest != digest
