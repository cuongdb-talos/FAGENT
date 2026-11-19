from pathlib import Path
import gdstk

from CALIBRE.calibre import CalibreSimulator, main
from FAGENT.tools.drc import DrcParser


ROOT = Path(__file__).resolve().parent.parent
DRC_DECK = ROOT / "DRC" / "1P7M_6Ic_1TKMc_ALPA2.drc"
SAMPLE_GDS = ROOT / "DRC" / "ROM_MCELL0.gds"


def test_calibre_simulator_on_sample_gds(tmp_path):
    sim = CalibreSimulator(gds_path=SAMPLE_GDS, drc_path=DRC_DECK, outdir=tmp_path).run()

    assert sim.top_cell == "ROM_MCELL0"
    assert len(sim.rules) > 800
    assert sim.results_path is not None and sim.results_path.is_file()
    assert sim.summary_path is not None and sim.summary_path.is_file()

    parsed_results = DrcParser.read_drc(sim.results_path)
    parsed_summary = DrcParser.read_drc(sim.summary_path)
    assert parsed_results["top_cell"] == "ROM_MCELL0"
    assert parsed_summary["top_cell"] == "ROM_MCELL0"
    assert parsed_summary["total_violations"] == sim.total_violations


def test_calibre_simulator_detects_skew_and_cli(tmp_path):
    lib = gdstk.Library()
    cell = lib.new_cell("TEST_SKEW")
    cell.add(gdstk.Polygon([(0, 0), (1, 0), (0.5, 0.3)], layer=61, datatype=0))
    gds_file = tmp_path / "test_skew.gds"
    lib.write_gds(str(gds_file))

    outdir = tmp_path / "out"
    rc = main(["--gds", str(gds_file), "--drc", str(DRC_DECK), "--outdir", str(outdir)])
    assert rc == 0

    parsed_summary = DrcParser.read_drc(outdir / "TEST_SKEW.drc.summary")
    assert parsed_summary["total_violations"] >= 1
    assert "DGR_3" in parsed_summary["violations_by_rule"]
