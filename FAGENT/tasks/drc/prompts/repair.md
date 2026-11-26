---
name: drc-layout-repair
description: >
  Interpret DRC rule text and report descriptions for a layout marker, and
  execute or report geometry repairs using fix_cover, fix_area, and fix_width. Use when
  acting as a CAD/EDA layout DRC repair agent to evaluate coverage, enclosure,
  minimum polygon area, or minimum polygon width violations on GDS layouts.
---

# DRC Layout Repair

All coordinates and distances are in GDS user units. Match every polygon ID to its layer using `layer_map`, and copy `layout.polygons[].id` verbatim into tool arguments.

## Steps

1. Inspect the input context for the current marker:
   - Read the complete rule text and DRC report description.
   - Check the configured goal, constraints, `layer_map`, and editable polygons in `layout.polygons[]`.
   - Note that the DRC marker area describes the marker geometry, not the target polygon area.

2. Evaluate layer status and missing layers using `layout.layer_status_in_search_region`:
   - Treat an empty layer as an empty set only if the search region is confirmed to cover the geometry required by the rule.
   - Treat any unknown or unmapped layer as having undetermined geometry.

3. Check representability and exceptions before calling any repair tool:
   - Preserve directional conditions, exclusions (such as `NOT INSIDE`), and the exact scope of each Boolean operator.
   - Only report the marker unresolved when a required condition cannot be represented by the available tools, or when critical inputs (such as target polygon IDs, layer mapping, or required thresholds) are missing and cannot be resolved from the current layout state or user review. When a user review is provided, use it together with the previous state and tool output to attempt a fix before giving up. If reporting unresolved, state the exact missing input or unsupported condition.".

4. Select and invoke the repair tool according to the rule type:
   - **Minimum polygon area rules**: Use `fix_area` with the target `polygon_id` and rule-specified minimum area.
   - **Minimum polygon width rules**: Use `fix_width` with the target `polygon_id` and rule-specified `minimum_width` in GDS user units. Keep the required width even when another instruction prefers smaller geometry.
   - **Oversized cover polygon after `fix_cover`**: Use `fix_area` with the covering polygon as `polygon_id` and `cover_context: {"protected_inner_ids": [...], "minimum_enclosure": ...}`. The tool proposes a grid-snapped contraction that retains enclosure; apply only a returned proposal and let `check_area` verify it. This contraction supports axis-aligned rectangular cover polygons; unsupported geometry returns `unknown` without an edit.
   - **Coverage and enclosure rules**: Use `fix_cover`:
     - Combine polygons within a single group using **OR** (an empty operand contributes no polygons; evaluate using remaining operands).
     - Separate groups express **AND** (every group must cover the inner polygon; an empty required group means coverage fails).
     - Use `minimum_enclosure: 0` for full-coverage rules with no additional distance requirement.

5. Handle tool responses and verification:
   - If a required AND group cannot be represented or repaired, report the missing required coverage.
   - If a repair fails or rolls back, use the returned error and review feedback to choose another supported repair tool before reporting unresolved.
   - Report unresolved only when required evidence is missing or no supported repair/check can satisfy the rule after attempting available options.
   - Do not claim a rule passes based on a partial check when an unrepresented condition remains.

## Rules

- Never invent rule limits, coordinates, or polygon IDs.
- Copy `layout.polygons[].id` verbatim into tool arguments.
- Preserve all directional conditions, Boolean operator scopes, and exceptions (`NOT INSIDE`).
- Always report `unresolved` with a specific reason when evidence is missing, tools return `unknown`, or rule conditions exceed tool capabilities.
- Never mark a rule as passed on a partial check if any condition in the rule text could not be represented.
