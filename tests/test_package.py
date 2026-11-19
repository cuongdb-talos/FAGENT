"""Exercise both entry points and task assets from an installed wheel."""

# === SECTION 1: Imports ===
from pathlib import Path
from shutil import copytree, ignore_patterns
import subprocess
import sys
import venv

import pytest


# === SECTION 2: Wheel tests ===
class TestPackage:
    """Build and install the distribution without fetching dependencies."""

    @pytest.fixture(scope="class")
    def installation(self, tmp_path_factory):
        """Build in scratch storage and install into a local environment."""

        workspace = tmp_path_factory.mktemp("wheel-install")
        source = workspace / "source"
        root = Path(__file__).resolve().parents[1]
        source.mkdir()
        (source / "pyproject.toml").write_bytes((root / "pyproject.toml").read_bytes())
        for name in ("FAGENT", "CALIBRE"):
            copytree(root / name, source / name, ignore=ignore_patterns("__pycache__", "*.sqlite3"))
        subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps",
                        "--no-build-isolation", "--wheel-dir", str(workspace), str(source)], check=True)
        environment = workspace / "env"
        venv.EnvBuilder(with_pip=False, system_site_packages=True).create(environment)
        python = environment / "bin/python"
        wheel = next(workspace.glob("*.whl"))
        subprocess.run([sys.executable, "-m", "pip", "--python", str(python), "install", "--no-deps",
                        "--force-reinstall", str(wheel)], check=True, cwd=workspace)
        return workspace, python

    def test_installed_package_loads_task_assets(self, installation):
        """Load installed DRC and LVS definitions and their referenced assets."""
        workspace, python = installation
        command = (
            "from pathlib import Path\nfrom FAGENT.fagent import Fagent\n"
            "for name in ('drc', 'lvs'):\n"
            "    assert Fagent().load_task_workflow(name, {}, Path.cwd()).steps[0].function\n"
        )
        subprocess.run([str(python), "-c", command], check=True, cwd=workspace)

    def test_both_cli_entry_points_show_verbose_help(self, installation):
        """Confirm both entry points expose the same flag."""
        workspace, python = installation
        for command in ([str(python), "-m", "FAGENT"], [str(python.with_name("fagent"))]):
            result = subprocess.run([*command, "--help"], check=True, cwd=workspace,
                                    text=True, capture_output=True)
            assert "--verbose" in result.stdout
