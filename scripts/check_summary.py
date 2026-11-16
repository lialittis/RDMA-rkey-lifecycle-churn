#!/usr/bin/env python3
"""Machine-check that the committed results table agrees with the raw
archived analysis.

Usage: python3 scripts/check_summary.py docs/reproduction-results.md
           results/analysis.json

Exits 0 only when the "## Results table" pipe table in the markdown
document agrees with analysis.json:

- exactly one primary row (the main run) and zero or one auxiliary row
  (the internal diagnostic smoke run, whose user-facing label begins with
  "shakedown");
- per-row numeric agreement: Regs, Wall (s, within 0.05 s), Index reuse,
  Full-key recurrence, Sampled outcomes counts and Stop reason all match
  the analysis run for that role, and row labels match exactly;
- limits within the authorized caps (primary <= 600000, auxiliary <= 4096
  registrations, <= 900 s), 64-hex source/binary hash columns,
  Verification == PASS, non-empty Deviations/Limitations cells;
- the aggregate block: total successful registrations equal to the sum of
  the table's Regs, within the 604096 aggregate ceiling, and the wall
  budget flags of the archived analysis not violated.

Any disagreement is reported as a "violation:" line on stderr and the
exit code is 1 (2 for usage/IO errors).  Output is deterministic: no
wall-clock time is embedded.  Hash integrity of the underlying artifacts
is verify.py's job; this check binds the committed table to the archived
analysis only."""

import argparse
import json
import os
import re
import sys

TABLE_COLUMNS = {"Run", "Role", "Kernel", "RXE", "Toolchain",
                 "Source SHA-256", "Binary SHA-256", "Reg limit", "Regs",
                 "Time limit (s)", "Wall (s)", "Index reuse",
                 "Full-key recurrence", "Sampled outcomes", "Stop reason",
                 "Verification", "Deviations", "Limitations"}
REQUIRED_COLUMNS = TABLE_COLUMNS

RUN_ROLES = ("smoke", "main")
ROLE_MARKING = {"main": "primary", "smoke": "auxiliary"}
ROLE_REG_CAPS = {"main": 600000, "smoke": 4096}
AGGREGATE_MAX_TARGET_REGISTRATIONS = 604096
MAX_WALL_SECONDS = 900
WALL_TOLERANCE_SECONDS = 0.05
AUXILIARY_USER_FACING = "shakedown"

STOP_REASONS = {"time-budget", "count-budget", "control-failure",
                "cleanup-warning", "memory-mismatch", "probe-timeout",
                "explicit-stop"}
OUTCOME_TOKENS = ("accepted", "rejected", "indeterminate", "anomalous")
OUTCOME_LINE = re.compile(
    r"^accepted=(\d+) rejected=(\d+) indeterminate=(\d+) anomalous=(\d+)$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
INT_CELL = re.compile(r"^\d+$")
FLOAT_CELL = re.compile(r"^\d+(\.\d+)?$")


class Violations(object):

    def __init__(self):
        self.items = []

    def add(self, message):
        self.items.append(message)

    def ok(self):
        return not self.items


def parse_table(text):
    """Return (header_cells, rows) of the pipe table under the
    '## Results table' heading, or raise ValueError with the reason."""
    lines = text.splitlines()
    heading = None
    for index, line in enumerate(lines):
        if line.strip() == "## Results table":
            heading = index
            break
    if heading is None:
        raise ValueError("no '## Results table' heading in the document")
    header = None
    rows = []
    index = heading + 1
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if not line.startswith("|"):
            if header is None:
                index += 1
                continue
            break
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if header is None:
            header = cells
        elif all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            pass
        else:
            rows.append(cells)
        index += 1
    if header is None:
        raise ValueError("no pipe table under the '## Results table' "
                         "heading")
    return header, rows


def outcome_counts(run):
    counts = {name: 0 for name in OUTCOME_TOKENS}
    for sample in run.get("samples", []):
        classification = sample.get("classification")
        if classification in counts:
            counts[classification] += 1
    return counts


def check(table_path, analysis_path):
    v = Violations()
    with open(table_path, "r", encoding="utf-8") as f:
        text = f.read()
    with open(analysis_path, "r", encoding="utf-8") as f:
        analysis = json.load(f)

    try:
        header, rows = parse_table(text)
    except ValueError as exc:
        v.add(str(exc))
        return v, 0

    if set(header) != REQUIRED_COLUMNS or len(header) != len(REQUIRED_COLUMNS):
        v.add("table header must contain exactly the columns %r, got %r"
              % (sorted(REQUIRED_COLUMNS), header))
        return v, 0
    col = {name: index for index, name in enumerate(header)}

    runs_by_role = {}
    for run in analysis.get("runs", []):
        runs_by_role.setdefault(run.get("run_role"), run)
    if "main" not in runs_by_role:
        v.add("analysis has no main run to compare the primary row against")
        return v, 0

    primary_rows = [r for r in rows
                    if r[col["Role"]] == ROLE_MARKING["main"]]
    auxiliary_rows = [r for r in rows
                      if r[col["Role"]] == ROLE_MARKING["smoke"]]
    unknown = [r for r in rows
               if r[col["Role"]] not in
               (ROLE_MARKING["main"], ROLE_MARKING["smoke"])]
    if len(primary_rows) != 1:
        v.add("exactly one primary row is required, found %d"
              % len(primary_rows))
    for row in unknown:
        v.add("row %r has an unknown Role marking %r (primary/auxiliary "
              "only; never a pilot)"
              % (row[col["Run"]], row[col["Role"]]))
    if len(auxiliary_rows) > 1:
        v.add("at most one auxiliary row is allowed, found %d"
              % len(auxiliary_rows))
    if auxiliary_rows and "smoke" not in runs_by_role:
        v.add("auxiliary row present but the analysis has no smoke run")
    if not auxiliary_rows and "smoke" in runs_by_role:
        v.add("the analysis has a smoke run but the table has no "
              "auxiliary row for it")

    regs_sum = 0
    for row in rows:
        marking = row[col["Role"]]
        role = "main" if marking == ROLE_MARKING["main"] else "smoke"
        run = runs_by_role.get(role)
        label = row[col["Run"]]
        if run is None:
            continue
        if label != run.get("label"):
            v.add("row %r does not match the analysis %s run label %r"
                  % (label, role, run.get("label")))
        if role == "smoke" and not label.startswith(AUXILIARY_USER_FACING):
            v.add("auxiliary row label %r must begin with the user-facing "
                  "name %r (the internal role name must not leak into "
                  "user-facing docs)" % (label, AUXILIARY_USER_FACING))
        # numeric agreement
        regs_cell = row[col["Regs"]]
        if INT_CELL.fullmatch(regs_cell):
            regs = int(regs_cell)
            regs_sum += regs
            if regs != run.get("target_registrations_succeeded"):
                v.add("row %r Regs %s != analysis %r"
                      % (label, regs_cell,
                         run.get("target_registrations_succeeded")))
        else:
            v.add("row %r Regs must be a non-negative integer, got %r"
                  % (label, regs_cell))
        wall_cell = row[col["Wall (s)"]]
        if FLOAT_CELL.fullmatch(wall_cell):
            wall = float(wall_cell)
            actual = run.get(
                "wall_seconds_first_registration_to_final_cleanup")
            if actual is None or abs(wall - actual) > WALL_TOLERANCE_SECONDS:
                v.add("row %r Wall (s) %s disagrees with analysis %r"
                      % (label, wall_cell, actual))
        else:
            v.add("row %r Wall (s) must be a number, got %r"
                  % (label, wall_cell))
        for cell_name, field in (("Index reuse",
                                  "index_recurrences_observed"),
                                 ("Full-key recurrence",
                                  "full_key_recurrences_observed")):
            cell = row[col[cell_name]]
            if INT_CELL.fullmatch(cell):
                if int(cell) != run.get(field):
                    v.add("row %r %s %s != analysis %r"
                          % (label, cell_name, cell, run.get(field)))
            else:
                v.add("row %r %s must be a non-negative integer, got %r"
                      % (label, cell_name, cell))
        # sampled outcomes
        outcomes = " ".join(row[col["Sampled outcomes"]].split())
        match = OUTCOME_LINE.fullmatch(outcomes)
        if not match:
            v.add("row %r Sampled outcomes must be 'accepted=A rejected=R "
                  "indeterminate=I anomalous=M', got %r"
                  % (label, row[col["Sampled outcomes"]]))
        else:
            expected = outcome_counts(run)
            shown = {name: int(value) for name, value
                     in zip(OUTCOME_TOKENS, match.groups())}
            if shown != expected:
                v.add("row %r Sampled outcomes %r != analysis counts %r "
                      "(samples=%d)"
                      % (label, outcomes,
                         " ".join("%s=%d" % (n, expected[n])
                                  for n in OUTCOME_TOKENS),
                         len(run.get("samples", []))))
        # stop reason
        stop = row[col["Stop reason"]]
        if stop not in STOP_REASONS:
            v.add("row %r Stop reason %r is not a valid stop reason"
                  % (label, stop))
        elif stop != run.get("stop_reason"):
            v.add("row %r Stop reason %r != analysis %r"
                  % (label, stop, run.get("stop_reason")))
        # limits
        limit_cell = row[col["Reg limit"]]
        if INT_CELL.fullmatch(limit_cell):
            limit = int(limit_cell)
            cap = ROLE_REG_CAPS[role]
            if limit > cap:
                v.add("row %r Reg limit %d exceeds the %s role cap %d"
                      % (label, limit, role, cap))
        else:
            v.add("row %r Reg limit must be a non-negative integer, got %r"
                  % (label, limit_cell))
        time_cell = row[col["Time limit (s)"]]
        if INT_CELL.fullmatch(time_cell):
            if int(time_cell) > MAX_WALL_SECONDS:
                v.add("row %r Time limit (s) %s exceeds the %d s budget"
                      % (label, time_cell, MAX_WALL_SECONDS))
        else:
            v.add("row %r Time limit (s) must be a non-negative integer, "
                  "got %r" % (label, time_cell))
        # provenance/format cells
        for cell_name in ("Source SHA-256", "Binary SHA-256"):
            cell = row[col[cell_name]]
            if not SHA256.fullmatch(cell):
                v.add("row %r %s must be 64 lowercase hex, got %r"
                      % (label, cell_name, cell[:80]))
        for cell_name in ("Kernel", "RXE", "Toolchain", "Deviations",
                          "Limitations"):
            if not row[col[cell_name]]:
                v.add("row %r %s cell must not be empty"
                      % (label, cell_name))
        if row[col["Verification"]] != "PASS":
            v.add("row %r Verification must be PASS, got %r"
                  % (label, row[col["Verification"]]))

    aggregate = analysis.get("aggregate")
    if not isinstance(aggregate, dict):
        v.add("analysis has no aggregate block")
    else:
        total = aggregate.get("target_registrations_succeeded_total")
        if total != regs_sum:
            v.add("aggregate total %r != the sum of the table Regs (%d)"
                  % (total, regs_sum))
        if not isinstance(total, int) or \
                total > AGGREGATE_MAX_TARGET_REGISTRATIONS:
            v.add("aggregate total %r exceeds the hard ceiling of %d "
                  "successful target registrations"
                  % (total, AGGREGATE_MAX_TARGET_REGISTRATIONS))
        if aggregate.get("within_registration_budget") is not True:
            v.add("the archived analysis reports the aggregate "
                  "registration budget as exceeded")
        if aggregate.get("within_wall_budget") is False:
            v.add("the archived analysis reports the aggregate wall "
                  "budget as exceeded")
        span = aggregate.get(
            "wall_seconds_first_registration_to_final_cleanup")
        if span is not None and span > MAX_WALL_SECONDS:
            v.add("aggregate wall span %r exceeds %d s"
                  % (span, MAX_WALL_SECONDS))

    return v, len(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check the committed results table against the "
                    "archived analysis")
    parser.add_argument("table", help="path to docs/reproduction-results.md")
    parser.add_argument("analysis", help="path to results/analysis.json")
    args = parser.parse_args(argv)
    for path in (args.table, args.analysis):
        if not os.path.isfile(path):
            sys.stderr.write("no such file: %s\n" % path)
            return 2
    try:
        violations, rows = check(args.table, args.analysis)
    except (OSError, ValueError) as exc:
        sys.stderr.write("check failed: %s\n" % exc)
        return 2
    for item in violations.items:
        sys.stderr.write("violation: %s\n" % item)
    if not violations.ok():
        sys.stderr.write("results table check FAILED (%d violations)\n"
                         % len(violations.items))
        return 1
    sys.stdout.write("results table check OK (%d rows)\n" % rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
