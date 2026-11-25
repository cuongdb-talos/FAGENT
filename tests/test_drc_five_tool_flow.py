"""Integration tests for the five-tool DRC queue, save routing, and end-to-end flow."""

# === SECTION 1: Imports ===
import json
from pathlib import Path

import gdstk

from FAGENT.capabilities.layout.cover_check import AreaCheck, CoverCheck
from FAGENT.capabilities.layout.input_loader import InputLoader
from FAGENT.engine.graph_runner import GraphRunner
from FAGENT.engine.graph_services import GraphServices
from FAGENT.engine.workflow_loader import WorkflowLoader


# === SECTION 2: Module-Level Helpers & Model Stubs ===
class DeterministicFiveToolModel:
    """Deterministic model stub exercising five-tool repair, unverified completion, and retries."""

    def run(self):
        return self

    def __init__(self, mode: str = "standard"):
        self.mode = mode
        self.calls = 0
        self.tool_names_seen = []
        self.stage_by_task = {}

    def chat(self, messages, specs, response_schema):
        self.calls += 1
        self.tool_names_seen.append([s["function"]["name"] for s in specs])
        last = messages[-1]
        prev_tool = messages[-1] if messages and messages[-1].get("role") == "tool" else None

        if prev_tool is not None:
            tool_out = json.loads(prev_tool["content"])
            # Advance propose -> apply -> check -> completed
            active_tid = next(reversed(self.stage_by_task), None)
            stage = self.stage_by_task.get(active_tid)
            if stage == "area_proposed" and tool_out.get("proposals"):
                pid = tool_out["proposals"][0]["proposal_id"]
                self.stage_by_task[active_tid] = "area_applied"
                return {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": f"call-app-area-{self.calls}",
                        "type": "function",
                        "function": {
                            "name": "fix_area",
                            "arguments": json.dumps({"task_id": active_tid, "action": "apply", "proposal_id": pid}),
                        },
                    }],
                }
            if stage == "area_applied":
                self.stage_by_task[active_tid] = "done"
                return {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": f"call-chk-area-{self.calls}",
                        "type": "function",
                        "function": {"name": "check_area", "arguments": json.dumps({"task_id": active_tid})},
                    }],
                }
            if stage == "cover_proposed" and tool_out.get("proposals"):
                pid = tool_out["proposals"][0]["proposal_id"]
                self.stage_by_task[active_tid] = "cover_applied"
                return {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": f"call-app-cov-{self.calls}",
                        "type": "function",
                        "function": {
                            "name": "fix_cover",
                            "arguments": json.dumps({"task_id": active_tid, "action": "apply", "proposal_id": pid}),
                        },
                    }],
                }
            if stage == "cover_applied":
                self.stage_by_task[active_tid] = "done"
                return {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": f"call-chk-cov-{self.calls}",
                        "type": "function",
                        "function": {"name": "check_cover", "arguments": json.dumps({"task_id": active_tid})},
                    }],
                }
            return {
                "role": "assistant",
                "content": json.dumps({
                    "decision": "completed",
                    "status": "incomplete",
                    "reason": "Completed local check",
                    "violations_analysis": [],
                    "recommendations": "Run full DRC deck",
                }),
            }

        if last["role"] == "user":
            payload = json.loads(last["content"])
            tid = payload["task_id"]
            rule = payload["violation"]["rule"]
            polys = payload["layout"]["polygons"]

            if self.mode == "unverified_claim":
                return {
                    "role": "assistant",
                    "content": json.dumps({
                        "decision": "completed",
                        "status": "pass",
                        "reason": "Claiming pass without check",
                        "violations_analysis": [],
                        "recommendations": "None",
                    }),
                }

            if self.mode == "bounded_retry":
                # Keep calling check_area without applying to exhaust max_iterations
                return {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": f"call-retry-{self.calls}",
                        "type": "function",
                        "function": {"name": "check_area", "arguments": json.dumps({"task_id": tid})},
                    }],
                }

            if rule == "GT_A_13" and polys:
                gt_poly = next((p for p in polys if p["lpp"][0] == 4), polys[0])
                self.stage_by_task[tid] = "area_proposed"
                return {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": f"call-prop-area-{self.calls}",
                        "type": "function",
                        "function": {
                            "name": "fix_area",
                            "arguments": json.dumps({
                                "task_id": tid,
                                "action": "propose",
                                "polygon_id": gt_poly["id"],
                                "minimum_area": 0.038,
                            }),
                        },
                    }],
                }

            if rule == "CT_R_11" and polys:
                violation = payload["violation"]
                assert violation["evidence"]["supported"] is True
                assert violation["requirement"]["min_enclosure"] == 0
                assert violation["cover_groups"] == [["M1"], ["GT", "AA"]]
                assert violation["exception_layers"] == ["INST", "EXCLU"]
                system = messages[0]["content"]
                assert system.count("Minimize width") == 1
                assert "read_gds" not in system and "replace_polygon" not in system
                ct_poly = next((p for p in polys if p["lpp"][0] == 1), None)
                m1_poly = next((p for p in polys if p["lpp"][0] == 2), None)
                aa_poly = next((p for p in polys if p["lpp"][0] == 3), None)
                if ct_poly and m1_poly and aa_poly:
                    proposal = violation["fix_cover_candidates"]["propose_arguments"]
                    assert proposal["inner_id"] == ct_poly["id"]
                    assert proposal["groups"] == [[m1_poly["id"]], [aa_poly["id"]]]
                    self.stage_by_task[tid] = "cover_proposed"
                    return {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": f"call-prop-cov-{self.calls}",
                            "type": "function",
                            "function": {
                                "name": "fix_cover",
                                "arguments": json.dumps(proposal),
                            },
                        }],
                    }

            return {
                "role": "assistant",
                "content": json.dumps({
                    "decision": "stop",
                    "status": "incomplete",
                    "reason": "Unsupported or ambiguous rule/geometry",
                    "violations_analysis": [],
                    "recommendations": "Manual review",
                }),
            }

        return {
            "role": "assistant",
            "content": json.dumps({
                "decision": "completed",
                "status": "incomplete",
                "reason": "Completed local check",
                "violations_analysis": [],
                "recommendations": "Run full DRC deck",
            }),
        }


def build_drc_run_inputs(
    tmp_path: Path, rules_text: list[str], cell_name: str = "CELL0", m1_right: float = 0.20
) -> tuple[Path, Path]:
    """Create GDS, DRC results, layermap, and rule deck, then run InputLoader."""
    gds_path = tmp_path / f"{cell_name}.gds"
    lib = gdstk.Library(unit=1e-6, precision=1e-9)
    cell = lib.new_cell(cell_name)
    cell.add(gdstk.rectangle((0.10, 0.10), (0.20, 0.20), layer=1, datatype=0))  # CT
    cell.add(gdstk.rectangle((0.10, 0.10), (m1_right, 0.20), layer=2, datatype=0))  # M1
    cell.add(gdstk.rectangle((0.10, 0.10), (0.20, 0.20), layer=3, datatype=0))  # AA
    cell.add(gdstk.rectangle((0.00, 0.00), (0.40, 0.06), layer=4, datatype=0))  # GT (area 0.024)
    lib.write_gds(str(gds_path))

    drc_path = tmp_path / f"{cell_name}.drc.results"
    drc_path.write_text("\n".join([f"{cell_name} 1000", *rules_text, ""]), encoding="utf-8")

    layermap = tmp_path / "tech.layermap"
    layermap.write_text("CT drawing 1 0\nM1 drawing 2 0\nAA drawing 3 0\nGT drawing 4 0\nINST drawing 5 0\nEXCLU drawing 6 0\n", encoding="utf-8")

    deck = tmp_path / "rules.drc"
    deck.write_text(
        "CT_R_11 {\n  @ CT covered by M1 and (AA or GT)\n  ((CT NOT (M1 AND (GT OR AA))) NOT INSIDE INST) NOT INSIDE EXCLU\n}\n"
        "GT_A_13 {\n  @ GT area >= 0.038um2\n  AREA GT < 0.038\n}\n"
        "M1_DEN {\n  @ M1 density\n  DENSITY M1 < 0.2\n}\n"
        "AMBIG_R {\n  @ Ambiguous rule\n  BAD_OP\n}\n",
        encoding="utf-8",
    )

    rundir = tmp_path / "rundir"
    InputLoader(gds_path=gds_path, drc_path=drc_path, output_dir=rundir, layer_map_files=[layermap], verbose=False).run()
    return rundir, deck


def run_drc_workflow(rundir: Path, deck: Path, model: DeterministicFiveToolModel, max_iterations: int = 8) -> dict:
    """Run the compiled DRC workflow with a deterministic model stub and no DRC backend."""
    workflow = WorkflowLoader(Path("FAGENT/tasks/drc/workflow.yaml")).run()
    config = {
        "goal": "Fix DRC violations.",
        "skip_prompt": "Skip density",
        "drc_files": [str(deck)],
        "system_prompt_file": "FAGENT/knowledge/templates/prompts/drc_site.md",
        "max_iterations": max_iterations,
        "rag_limit": 2,
        "rag_context_chars": 2000,
    }
    services = GraphServices(rundir, config, workflow, verbose=False)
    services.build_chat_client = lambda: model
    services.embed_texts = lambda texts: [[1, 0]] + [
        [1, 0] if "density" in text.lower() else [0, 1] for text in texts[1:]
    ]
    services.rag = type("EmptyRag", (), {"retrieve": lambda self, q, r, l: []})()
    messages = services.prepare(resume=False)
    runner = GraphRunner(services, rundir)
    gds_info = json.loads((rundir / "gds.json").read_text(encoding="utf-8"))
    drc_info = json.loads((rundir / "drc.json").read_text(encoding="utf-8"))
    return runner.run(
        messages,
        {
            "gds": gds_info["source"],
            "drc": drc_info["source"],
            "layer_map": gds_info.get("layer_maps", []),
        },
    )


# === SECTION 3: Integration Tests ===
def test_five_tool_workflow_exposes_only_five_tools_and_saves_once_with_unresolved(tmp_path: Path, monkeypatch) -> None:
    """Density is skipped, supported area/cover pass, ambiguous stays unresolved, and save runs once."""
    rundir, deck = build_drc_run_inputs(
        tmp_path,
        [
            "M1_DEN", "1 1 1 Nov 25 12:00:00 2026", "Density", "p 1 4", "0 0", "100 0", "100 100", "0 100",
            "GT_A_13", "1 1 1 Nov 25 12:00:00 2026", "Area", "p 1 4", "0 0", "400 0", "400 60", "0 60",
            "AMBIG_R", "1 1 1 Nov 25 12:00:00 2026", "Ambiguous", "p 1 4", "0 0", "50 0", "50 50", "0 50",
        ],
    )
    model = DeterministicFiveToolModel()
    result = run_drc_workflow(rundir, deck, model)

    expected_tools = {"fix_cover", "fix_area", "check_cover", "check_area", "save_gds"}
    assert model.tool_names_seen
    for names in model.tool_names_seen:
        assert set(names) == expected_tools

    tasks = result["checklist"]["tasks"]
    by_rule = {t["rule"]: t for t in tasks}
    assert "M1_DEN" not in by_rule
    assert by_rule["GT_A_13"]["status"] == "pass"
    assert by_rule["AMBIG_R"]["status"] == "unresolved"
    assert result["status"] == "incomplete"
    assert result["validation"] is None

    saved_gds = rundir / "candidate.gds"
    assert saved_gds.is_file()
    assert GraphServices.candidate_digest(saved_gds) == result["candidate_digest"]
    reopened = gdstk.read_gds(str(saved_gds))
    gt_polys = [p for p in reopened.cells[0].polygons if p.layer == 4]
    assert AreaCheck().run(gt_polys[0], minimum_area=0.038, precision=0.001)["status"] == "pass"


def test_unverified_agent_completion_cannot_mark_pass(tmp_path: Path) -> None:
    """Agent returning pass without check_cover/check_area tied to candidate is marked unresolved."""
    rundir, deck = build_drc_run_inputs(
        tmp_path,
        ["GT_A_13", "1 1 1 Nov 25 12:00:00 2026", "Area", "p 1 4", "0 0", "400 0", "400 60", "0 60"],
    )
    model = DeterministicFiveToolModel(mode="unverified_claim")
    result = run_drc_workflow(rundir, deck, model)
    assert result["checklist"]["tasks"][0]["status"] == "unresolved"


def test_bounded_retries_marks_unresolved(tmp_path: Path) -> None:
    """Exceeding max_iterations marks the active task unresolved and finishes cleanly."""
    rundir, deck = build_drc_run_inputs(
        tmp_path,
        ["GT_A_13", "1 1 1 Nov 25 12:00:00 2026", "Area", "p 1 4", "0 0", "400 0", "400 60", "0 60"],
    )
    model = DeterministicFiveToolModel(mode="bounded_retry")
    result = run_drc_workflow(rundir, deck, model, max_iterations=2)
    assert result["checklist"]["tasks"][0]["status"] == "unresolved"


def test_empty_queue_routes_directly_to_save_once(tmp_path: Path) -> None:
    """When only density rules exist (empty active queue), agent is not called and save runs once."""
    rundir, deck = build_drc_run_inputs(
        tmp_path,
        ["M1_DEN", "1 1 1 Nov 25 12:00:00 2026", "Density", "p 1 4", "0 0", "100 0", "100 100", "0 100"],
    )
    model = DeterministicFiveToolModel()
    result = run_drc_workflow(rundir, deck, model)
    assert model.calls == 0
    assert (rundir / "candidate.gds").is_file()
    assert result["candidate_digest"] == GraphServices.candidate_digest(rundir / "candidate.gds")



def test_boolean_cover_rule_repairs_ct_through_five_tool_flow(tmp_path: Path) -> None:
    """The boolean rule reaches fix_cover, applies a proposal, and passes local verification."""
    rundir, deck = build_drc_run_inputs(
        tmp_path,
        ["CT_R_11", "1 1 1 Nov 25 12:00:00 2026", "Cover", "p 1 4", "175 100", "200 100", "200 200", "175 200"],
        m1_right=0.18,
    )
    result = run_drc_workflow(rundir, deck, DeterministicFiveToolModel())

    task, = result["checklist"]["tasks"]
    assert task["status"] == "pass"
    assert result["validation"] is None
    saved = gdstk.read_gds(str(rundir / "candidate.gds"))
    m1, = [poly for poly in saved.cells[0].polygons if poly.layer == 2]
    assert m1.bounding_box()[1][0] == 0.20
