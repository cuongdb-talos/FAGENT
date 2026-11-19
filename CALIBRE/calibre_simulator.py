"""Module CALIBRE.calibre: Bộ giả lập Calibre DRC (Calibre DRC Simulator) cho kiểm thử và mô phỏng.

Trách nhiệm & Chức năng:
- Cung cấp lớp `CalibreSimulator` mô phỏng quy trình chạy Calibre DRC độc lập không phụ thuộc license công cụ:
  + Phân tích cú pháp rule deck Calibre SVRF (layer definitions, rule checks, spacing, width, enclosure).
  + Đọc file layout GDS thông qua thư viện gdstk, kiểm tra các điều kiện hình học của polygon.
  + Sinh ra tệp kết quả ASCII DRC Results (`.drc.results`) chứa marker vi phạm và tệp tóm tắt (`.drc.summary`).
- Phục vụ môi trường CI/CD, kiểm thử tự động và giả lập xác thực trong luồng FAGENT DRC Auto-Repair.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import math
from pathlib import Path
import re
import time
import gdstk


# --- CalibreSimulator: standalone SVRF parser, gdstk geometry checker, and report writer ---
class CalibreSimulator:
    """Simulate Calibre DRC execution using a SVRF rule deck and a GDS file."""

    # --- Entry point: orchestrate deck parsing, GDS loading, checks, and report output ---
    def run(self) -> "CalibreSimulator":
        """Execute the full simulated Calibre DRC flow and store outputs in self."""
        start_time = time.time()
        self.parse_drc_deck()
        self.load_gds()
        self.check_geometry()
        self.write_results(start_time)
        self.write_summary(start_time)
        self.status = "incomplete"
        return self

    def __init__(
        self,
        gds_path: str | Path,
        drc_path: str | Path,
        outdir: str | Path | None = None,
        top_cell: str | None = None,
        angle_tolerance_deg: float = 0.1,
    ) -> None:
        self.gds_path = Path(gds_path).resolve()
        self.drc_path = Path(drc_path).resolve()
        self.outdir = Path(outdir).resolve() if outdir else self.gds_path.parent / "calibre_sim"
        self.requested_top_cell = top_cell
        self.angle_tolerance_deg = float(angle_tolerance_deg)

        self.precision = 1000
        self.layer_map: dict[tuple[int, int], list[str]] = {}
        self.layer_names: dict[str, list[tuple[int, int]]] = {}
        self.rules: list[dict] = []

        self.library = None
        self.top_cell = ""
        self.dbu_per_user_unit = 1000
        self.layer_counts: dict[tuple[int, int], int] = {}
        self.named_layer_counts: dict[str, int] = {}
        self.violations: dict[str, list[list[tuple[int, int]]]] = {}
        self.total_violations = 0
        self.results_path: Path | None = None
        self.summary_path: Path | None = None
        self.status = "not_run"
        self.checked_rules: list[str] = []

    # --- SVRF deck parser: extract precision, layer mappings, and rule blocks ---
    def parse_drc_deck(self) -> None:
        """Parse precision, layer mappings, and rule blocks from the DRC deck."""
        if not self.drc_path.is_file():
            raise FileNotFoundError(f"DRC file not found: {self.drc_path}")

        lines = self.drc_path.read_text(errors="replace").splitlines()
        id_to_gds: dict[str, list[tuple[int, int]]] = {}
        id_to_names: dict[str, list[str]] = {}

        prec_re = re.compile(r"^\s*PRECISION\s+(\d+)\b", re.IGNORECASE)
        map_re = re.compile(
            r"^\s*LAYER\s+MAP\s+(\d+)\s+(?:DATATYPE|TEXTTYPE)\s+(\d+)\s+(\S+)",
            re.IGNORECASE,
        )
        layer_re = re.compile(r"^\s*LAYER\s+([A-Za-z0-9_]+)\s+(\S+)\s*$", re.IGNORECASE)
        rule_start_re = re.compile(r"^\s*([A-Za-z0-9_]+)\s*\{\s*$")

        current_rule: dict | None = None
        brace_depth = 0

        for line in lines:
            if current_rule is not None:
                brace_depth += line.count("{") - line.count("}")
                stripped = line.strip()
                if stripped.startswith("@"):
                    desc = stripped[1:].strip()
                    if desc:
                        current_rule["description"].append(desc)
                elif stripped and not stripped.startswith("}"):
                    current_rule["body"].append(stripped)
                if brace_depth <= 0:
                    self.rules.append(current_rule)
                    current_rule = None
                    brace_depth = 0
                continue

            m_prec = prec_re.match(line)
            if m_prec:
                self.precision = int(m_prec.group(1))
                continue

            m_map = map_re.match(line)
            if m_map:
                layer_num, datatype, layer_id = int(m_map.group(1)), int(m_map.group(2)), m_map.group(3)
                id_to_gds.setdefault(layer_id, []).append((layer_num, datatype))
                continue

            m_layer = layer_re.match(line)
            if m_layer:
                name, layer_id = m_layer.group(1), m_layer.group(2)
                if name.upper() != "MAP":
                    id_to_names.setdefault(layer_id, []).append(name)
                continue

            m_rule = rule_start_re.match(line)
            if m_rule:
                current_rule = {
                    "name": m_rule.group(1),
                    "description": [],
                    "body": [],
                }
                brace_depth = 1

        for layer_id, pairs in id_to_gds.items():
            names = id_to_names.get(layer_id, [])
            for pair in pairs:
                self.layer_map.setdefault(pair, [])
                for name in names:
                    if name not in self.layer_map[pair]:
                        self.layer_map[pair].append(name)
            for name in names:
                self.layer_names.setdefault(name, [])
                for pair in pairs:
                    if pair not in self.layer_names[name]:
                        self.layer_names[name].append(pair)

    # --- GDS loader: read layout, flatten top cell, and tally per-layer polygon counts ---
    def load_gds(self) -> None:
        """Load the GDS library, resolve top cell, and count geometries per layer."""
        if not self.gds_path.is_file():
            raise FileNotFoundError(f"GDS file not found: {self.gds_path}")

        self.library = gdstk.read_gds(str(self.gds_path))
        self.dbu_per_user_unit = max(1, round(self.library.unit / self.library.precision))

        if self.requested_top_cell:
            matches = [c for c in self.library.cells if c.name == self.requested_top_cell]
            if not matches:
                raise ValueError(f"Top cell '{self.requested_top_cell}' not found in {self.gds_path}")
            cell = matches[0]
        else:
            top_cells = self.library.top_level()
            cell = top_cells[0] if top_cells else self.library.cells[0]

        if not cell.name or Path(cell.name).name != cell.name or cell.name in ('.', '..'):
            raise ValueError(f'Invalid top-cell name for DRC reports: {cell.name!r}')
        self.top_cell = cell.name
        flat_cell = cell.copy(f"{cell.name}_flat")
        flat_cell.flatten()

        self.layer_counts = {}
        for poly in flat_cell.polygons:
            key = (poly.layer, poly.datatype)
            self.layer_counts[key] = self.layer_counts.get(key, 0) + 1

        self.named_layer_counts = {name: 0 for name in self.layer_names}
        for pair, count in self.layer_counts.items():
            for name in self.layer_map.get(pair, []):
                self.named_layer_counts[name] = self.named_layer_counts.get(name, 0) + count

        self._flat_polygons = flat_cell.polygons

    # --- Geometry checks: detect angle/skew, forbidden-layer, and sample rule violations ---
    def check_geometry(self) -> None:
        """Run lightweight geometric sanity checks mapped to matching DRC rules."""
        self.violations = {rule["name"]: [] for rule in self.rules}
        scale = self.precision or self.dbu_per_user_unit

        non_ortho_rule = self._find_rule(["ANGLE", "SKEW"])
        forbidden_rule = self._find_forbidden_layer_rule()

        for poly in self._flat_polygons:
            pts_dbu = [(round(x * scale), round(y * scale)) for x, y in poly.points]
            if non_ortho_rule and self._has_non_ortho_or_45(poly.points):
                self.violations[non_ortho_rule].append(pts_dbu)

            if forbidden_rule:
                layer_names = set(self.layer_map.get((poly.layer, poly.datatype), []))
                if layer_names & forbidden_rule[1]:
                    self.violations[forbidden_rule[0]].append(pts_dbu)

        self.check_sample_rules()
        self.total_violations = sum(len(items) for items in self.violations.values())

    def check_sample_rules(self) -> None:
        """Evaluate a limited geometric approximation of selected deck rules."""
        self.checked_rules = []
        rules = {rule["name"]: rule for rule in self.rules}
        layers = {name: [p for p in self._flat_polygons if (p.layer, p.datatype) in pairs]
                  for name, pairs in self.layer_names.items()}
        precision = 1 / self.dbu_per_user_unit
        scale = self.precision or self.dbu_per_user_unit
        excluded = layers.get("INST", []) + layers.get("EXCLUi", []) + layers.get("MARKS", [])
        metal = gdstk.boolean(layers.get("M1i", []), layers.get("M1SLOT", []), "not", precision=precision)

        for name, rule in rules.items():
            body = " ".join(rule["body"])
            area = re.search(r"AREA\s+(\w+)\s*<\s*([\d.]+)", body, re.I)
            border = re.search(r"ENC\s+(\w+)\s+BORDER\s*<\s*([\d.]+)", body, re.I)
            markers = None
            if name == "GT_A_13" and area:
                candidates = gdstk.boolean(layers.get(area.group(1), []), [], "or", precision=precision)
                markers = [p for p in candidates if p.area() < float(area.group(2))]
            elif name == "CT_R_11":
                support = gdstk.boolean(layers.get("GT", []) + layers.get("AA", []), metal, "and", precision=precision)
                markers = gdstk.boolean(layers.get("CT", []), support, "not", precision=precision)
            elif name == "CT_EN_7a_7b":
                threshold = re.search(r"ENC\s+CT\s+M1\s*<\s*([\d.]+)", body, re.I)
                if threshold:
                    expanded = gdstk.offset(layers.get("CT", []), float(threshold.group(1)), precision=precision)
                    markers = gdstk.boolean(expanded, metal, "not", precision=precision)
            elif name.startswith("BD_R_1_") and border and name in {"BD_R_1_AA", "BD_R_1_GT", "BD_R_1_CT", "BD_R_1_M1"}:
                expanded = gdstk.offset(layers.get(border.group(1), []), float(border.group(2)), precision=precision)
                markers = gdstk.boolean(expanded, layers.get("BORDER", []), "not", precision=precision)
            if markers is None:
                continue
            self.checked_rules.append(name)
            for polygon in markers:
                remainder = gdstk.boolean([polygon], excluded, "not", precision=precision)
                if remainder:
                    self.violations[name].append([(round(x * scale), round(y * scale)) for x, y in polygon.points])

    # --- Internal rule & angle helpers: match deck rules and check polygon edge angles ---
    def _find_rule(self, keywords: list[str]) -> str | None:
        for rule in self.rules:
            body_text = " ".join(rule["body"]).upper()
            if all(kw in body_text for kw in keywords):
                return rule["name"]
        return None

    def _find_forbidden_layer_rule(self) -> tuple[str, set[str]] | None:
        or_re = re.compile(r"\bOR\s+([A-Za-z0-9_\s]+)$")
        for rule in self.rules:
            body_text = " ".join(rule["body"])
            if "COPY DRC:1" in body_text.upper():
                for line in rule["body"]:
                    m = or_re.search(line)
                    if m:
                        return rule["name"], set(m.group(1).split())
        return None

    def _has_non_ortho_or_45(self, points) -> bool:
        n = len(points)
        if n < 3:
            return False
        tol = self.angle_tolerance_deg
        for i in range(n):
            x1, y1 = points[i]
            x2, y2 = points[(i + 1) % n]
            dx, dy = abs(x2 - x1), abs(y2 - y1)
            if dx == 0 and dy == 0:
                continue
            angle = math.degrees(math.atan2(dy, dx))
            if not (angle <= tol or abs(angle - 45.0) <= tol or abs(angle - 90.0) <= tol):
                return True
        return False

    # --- Report writers: emit Calibre ASCII .drc.results database and .drc.summary report ---
    def write_results(self, start_time: float) -> None:
        """Write Calibre ASCII DRC results database."""
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.results_path = self.outdir / f"{self.top_cell}.drc.results"
        timestamp = datetime.fromtimestamp(start_time).strftime("%b %d %H:%M:%S %Y")
        scale = self.precision or self.dbu_per_user_unit

        out_lines = [f"{self.top_cell} {scale}"]
        for rule in self.rules:
            name = rule["name"]
            markers = self.violations.get(name, [])
            count = len(markers)
            desc_lines = [f"Rule File Pathname: {self.drc_path}"] + rule["description"]
            out_lines.append(name)
            out_lines.append(f"{count} {count} {len(desc_lines)} {timestamp}")
            out_lines.extend(desc_lines)
            for idx, pts in enumerate(markers, start=1):
                out_lines.append(f"p {idx} {len(pts)}")
                out_lines.append(f"CN {self.top_cell} c 1 0 0 1 0 0 0")
                for x, y in pts:
                    out_lines.append(f"{x} {y}")

        self.results_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")

    def write_summary(self, start_time: float) -> None:
        """Write Calibre DRC summary report."""
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.summary_path = self.outdir / f"{self.top_cell}.drc.summary"
        elapsed = max(0, int(round(time.time() - start_time)))
        exec_dt = datetime.fromtimestamp(start_time).strftime("%a %b %d %H:%M:%S %Y")
        total_orig_geom = sum(self.layer_counts.values())

        layer_lines = []
        for name, count in self.named_layer_counts.items():
            dots = "." * max(3, 18 - len(name))
            layer_lines.append(f"LAYER {name} {dots} TOTAL Original Geometry Count = {count} ({count})")

        rulecheck_lines = []
        for rule in self.rules:
            name = rule["name"]
            cnt = len(self.violations.get(name, []))
            dots = "." * max(3, 28 - len(name))
            rulecheck_lines.append(f"RULECHECK {name} {dots} TOTAL Result Count = {cnt} ({cnt})")

        cell_dots = "." * max(3, 28 - len(self.top_cell))
        cell_lines = [
            f"CELL {self.top_cell} {cell_dots} TOTAL Result Count = {self.total_violations} ({self.total_violations})"
        ]
        for rule in self.rules:
            name = rule["name"]
            cnt = len(self.violations.get(name, []))
            if cnt > 0:
                dots = "." * max(3, 24 - len(name))
                cell_lines.append(f"    RULECHECK {name} {dots} TOTAL Result Count = {cnt} ({cnt})")

        template_path = Path(__file__).resolve().parent.parent / "FAGENT" / "knowledge" / "templates" / "reports" / "drc_summary.txt"
        template = template_path.read_text(encoding="utf-8")
        rendered = template.format(
            exec_dt=exec_dt,
            drc_path=self.drc_path,
            gds_path=self.gds_path,
            top_cell=self.top_cell,
            results_name=self.results_path.name if self.results_path else "",
            summary_name=self.summary_path.name,
            layer_stats="\n".join(layer_lines),
            rulecheck_stats="\n".join(rulecheck_lines),
            cell_stats="\n".join(cell_lines),
            elapsed=elapsed,
            total_orig_geom=total_orig_geom,
            total_rules=len(self.rules),
            total_violations=self.total_violations,
        )
        self.summary_path.write_text(rendered, encoding="utf-8")

    @staticmethod
    def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
        """Parse CLI arguments for standalone Calibre DRC simulation."""
        default_drc = Path(__file__).resolve().parent.parent / "DRC" / "1P7M_6Ic_1TKMc_ALPA2.drc"
        parser = argparse.ArgumentParser(description="Simulate Calibre DRC checking on a GDS file.")
        parser.add_argument("--gds", required=True, help="Path to input GDSII file")
        parser.add_argument("--drc", default=str(default_drc), help="Path to Calibre SVRF DRC rule file")
        parser.add_argument("--top", default=None, help="Top cell name (auto-detected if omitted)")
        parser.add_argument("--outdir", default=None, help="Output directory for .drc.results and .drc.summary")
        parser.add_argument("--angle-tol", type=float, default=0.1, help="Angle tolerance in degrees for 45/90 check")
        return parser.parse_args(argv)

    @classmethod
    def main(cls, argv: list[str] | None = None) -> int:
        """CLI entrypoint."""
        args = cls.parse_args(argv)
        sim = cls(
            gds_path=args.gds,
            drc_path=args.drc,
            outdir=args.outdir,
            top_cell=args.top,
            angle_tolerance_deg=args.angle_tol,
        )
        sim.run()
        print(f"[CalibreSim] Top cell: {sim.top_cell}")
        print(f"[CalibreSim] Rules checked: {len(sim.rules)}")
        print(f"[CalibreSim] Violations: {sim.total_violations}")
        print("[CalibreSim] INCOMPLETE: approximate geometry checks; not a full SVRF run")
        for name in sim.checked_rules:
            print(f"[CalibreSim] {name}: {len(sim.violations[name])}")
        print(f"[CalibreSim] Results: {sim.results_path}")
        print(f"[CalibreSim] Summary: {sim.summary_path}")
        return 0


if __name__ == "__main__":
    raise SystemExit(CalibreSimulator.main())
