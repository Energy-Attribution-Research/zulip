"""
parse_powermetrics.py
Converts macOS powermetrics text output → AMD µProf compatible timechart.csv

The output CSV must be EXACTLY compatible with analyst_agent.py:
  - Header: "PROFILE RECORDS," marker followed by column headers
  - Timestamps: H:M:S:mmm wall-clock format (matching AMD uProf format)
  - Power column: "Package Power (W)" matching find_power_column() pattern
  - Power values: in Watts (converted from mW)

The critical requirement is that timechart timestamps are WALL-CLOCK times,
not zero-based offsets, because deconvolve() in analyst_agent.py matches them
against exec_start/exec_end times from timestamps.csv.

Usage: python3 scripts/parse_powermetrics.py <run_id>
"""

import re
import sys
import json
from pathlib import Path
from datetime import datetime


# ---------------------------------------------------------------------------
# Validation constants — used for post-conversion sanity checks
# ---------------------------------------------------------------------------
# Any consumer/server CPU or SoC should fall within this range.
# Values outside indicate a parsing bug, not a real measurement.
MIN_PLAUSIBLE_WATTS = 0.0
MAX_PLAUSIBLE_WATTS = 500.0


def parse_power_samples(text: str) -> list[dict]:
    """
    Extract CPU Power (mW) and the wall-clock timestamp from each sample block.

    Each powermetrics sample block starts with a line like:
      *** Sampled system activity (Fri May 15 09:51:40 2026 +0530) (103.01ms elapsed) ***

    And contains a line like:
      CPU Power: 335 mW

    Returns list of dicts: { 'timestamp': datetime, 'watts': float }
    """
    samples = []

    # Split into sample blocks using the *** Sampled ... *** marker
    # Pattern for the header line with timestamp
    header_pattern = re.compile(
        r"\*\*\* Sampled system activity \((.+?)\) \(\d+\.\d+ms elapsed\) \*\*\*"
    )
    # Pattern for CPU Power line
    power_pattern = re.compile(r"CPU Power:\s+([\d.]+)\s+mW")

    # Split text into blocks by the *** marker
    blocks = re.split(r"(?=\*\*\* Sampled system activity)", text)

    for block in blocks:
        header_match = header_pattern.search(block)
        power_match = power_pattern.search(block)

        if header_match and power_match:
            # Parse timestamp: "Fri May 15 09:51:40 2026 +0530"
            ts_str = header_match.group(1)
            # Remove timezone offset for parsing, handle it separately
            # Format: "Fri May 15 09:51:40 2026 +0530"
            try:
                # Strip the timezone part for simple parsing
                # We only need H:M:S:mmm for the timechart
                ts_parts = ts_str.rsplit(" ", 1)  # Split off "+0530"
                ts_clean = ts_parts[0]  # "Fri May 15 09:51:40 2026"
                ts = datetime.strptime(ts_clean, "%a %b %d %H:%M:%S %Y")
            except ValueError:
                continue

            watts = float(power_match.group(1)) / 1000.0  # mW → W
            samples.append({"timestamp": ts, "watts": watts})

    return samples


def generate_timechart_csv(samples: list[dict]) -> str:
    """
    Generate AMD µProf-compatible timechart CSV with WALL-CLOCK timestamps.

    AMD uProf format:
      PROFILE RECORDS,
      RecordId,Timestamp,socket0-package-power,...
      1,20:58:19:282,    9.27,...

    Our simplified Mac format (compatible with analyst_agent.py):
      PROFILE RECORDS,
      Timestamp,Package Power (W)
      9:51:40:000,0.2330

    The Timestamp column uses H:M:S:mmm wall-clock format.
    analyst_agent.py's parse_timechart_timestamp() parses this as:
      parts = ts.split(":") → h, m, s, ms
    analyst_agent.py's deconvolve() converts exec_start/exec_end to:
      timedelta(hours=h, minutes=m, seconds=s, milliseconds=ms//1000)
    Then matches: df["_td"] >= exec_start_td & df["_td"] <= exec_end_td

    CRITICAL: Since powermetrics only gives second-level precision in its
    header timestamps, we use the sample index to estimate sub-second offsets
    when multiple samples share the same second.
    """
    lines = ["PROFILE RECORDS,", "Timestamp,Package Power (W)"]

    # Group samples by their second-level timestamp to distribute
    # millisecond offsets within each second
    if not samples:
        return "\n".join(lines) + "\n"

    # Track how many samples we've seen for each second
    second_counts: dict[str, int] = {}

    for sample in samples:
        ts = sample["timestamp"]
        watts = sample["watts"]

        # Create a key for this second
        sec_key = ts.strftime("%H:%M:%S")

        if sec_key not in second_counts:
            second_counts[sec_key] = 0
        else:
            second_counts[sec_key] += 1

        # Estimate millisecond offset: each sample within the same second
        # is ~100ms apart
        ms_offset = second_counts[sec_key] * 100

        # Format as H:M:S:mmm (matching AMD uProf format)
        # Note: AMD uses non-zero-padded values, e.g., "21:6:19:744"
        timestamp = f"{ts.hour}:{ts.minute}:{ts.second}:{ms_offset:03d}"
        lines.append(f"{timestamp},{watts:.4f}")

    return "\n".join(lines) + "\n"


def validate_samples(samples: list[dict], scenario: str) -> list[str]:
    """
    Post-conversion validation — ensures parsed data is plausible.
    Returns a list of warning/error strings. Empty list = all good.
    """
    issues = []

    if len(samples) == 0:
        issues.append(f"CRITICAL: {scenario} — 0 samples parsed. No power data captured.")
        return issues

    watts_values = [s["watts"] for s in samples]
    min_w = min(watts_values)
    max_w = max(watts_values)
    mean_w = sum(watts_values) / len(watts_values)

    # Check plausible range
    if min_w < MIN_PLAUSIBLE_WATTS:
        issues.append(f"WARNING: {scenario} — min power {min_w:.4f}W is below 0W (negative power?)")
    if max_w > MAX_PLAUSIBLE_WATTS:
        issues.append(f"WARNING: {scenario} — max power {max_w:.4f}W exceeds {MAX_PLAUSIBLE_WATTS}W plausibility threshold")

    # Check timestamp continuity
    first_ts = samples[0]["timestamp"]
    last_ts = samples[-1]["timestamp"]
    span_seconds = (last_ts - first_ts).total_seconds()

    if span_seconds <= 0:
        issues.append(f"WARNING: {scenario} — timestamp span is {span_seconds}s (zero or negative duration)")

    # Expected sample count at 100ms interval
    expected_samples = int(span_seconds / 0.1) + 1
    actual_samples = len(samples)
    if actual_samples < expected_samples * 0.5:
        issues.append(f"WARNING: {scenario} — only {actual_samples} samples for {span_seconds}s span "
                      f"(expected ~{expected_samples} at 100ms interval, >50% loss)")

    return issues


def process_run(run_id: str):
    """
    Process all scenarios for a given Mac run ID.
    Uses dynamic directory discovery instead of hardcoded scenario names —
    any subdirectory containing power_data.txt is treated as a scenario.
    """
    base = Path(__file__).parent.parent / "ResearchData" / "cpudata" / run_id

    if not base.exists():
        print(f"ERROR: Run directory not found: {base}")
        sys.exit(1)

    # Dynamic discovery: find all subdirectories with power_data.txt
    scenario_dirs = sorted([
        d for d in base.iterdir()
        if d.is_dir() and (d / "power_data.txt").exists()
    ])

    if not scenario_dirs:
        print(f"ERROR: No scenario directories with power_data.txt found in {base}")
        sys.exit(1)

    print(f"Discovered {len(scenario_dirs)} scenario(s): {[d.name for d in scenario_dirs]}")

    all_validation_issues = []
    conversion_summary = []

    for scenario_dir in scenario_dirs:
        scenario = scenario_dir.name
        raw_file = scenario_dir / "power_data.txt"
        output_file = scenario_dir / "timechart.csv"

        text = raw_file.read_text()
        samples = parse_power_samples(text)

        # Validate
        issues = validate_samples(samples, scenario)
        all_validation_issues.extend(issues)

        if not samples:
            print(f"  ❌ {scenario}: No power samples found!")
            conversion_summary.append({
                "scenario": scenario,
                "status": "FAILED",
                "samples": 0,
                "issues": issues,
            })
            continue

        csv_content = generate_timechart_csv(samples)
        output_file.write_text(csv_content)

        # Compute summary stats
        watts = [s["watts"] for s in samples]
        first_ts = samples[0]["timestamp"].strftime("%H:%M:%S")
        last_ts = samples[-1]["timestamp"].strftime("%H:%M:%S")

        summary = {
            "scenario": scenario,
            "status": "OK",
            "samples": len(samples),
            "time_range": f"{first_ts} → {last_ts}",
            "min_watts": round(min(watts), 4),
            "max_watts": round(max(watts), 4),
            "mean_watts": round(sum(watts) / len(watts), 4),
            "issues": issues,
        }
        conversion_summary.append(summary)

        print(f"  ✅ {scenario}: {len(samples)} samples ({first_ts} → {last_ts}) "
              f"| avg={summary['mean_watts']:.2f}W | range=[{summary['min_watts']:.2f}, {summary['max_watts']:.2f}]W "
              f"→ {output_file}")

    # Write validation report
    report_path = base / "conversion_report.json"
    report = {
        "run_id": run_id,
        "scenarios_found": len(scenario_dirs),
        "scenarios_converted": sum(1 for s in conversion_summary if s["status"] == "OK"),
        "validation_issues": all_validation_issues,
        "details": conversion_summary,
    }
    report_path.write_text(json.dumps(report, indent=2))
    print(f"\n📋 Conversion report written to: {report_path}")

    # Print validation summary
    if all_validation_issues:
        print(f"\n⚠️  {len(all_validation_issues)} validation issue(s):")
        for issue in all_validation_issues:
            print(f"    {issue}")
        sys.exit(1)
    else:
        print("\n✅ All validations passed — data is plausible.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 scripts/parse_powermetrics.py <run_id>")
        sys.exit(1)

    run_id = sys.argv[1]
    print(f"Parsing powermetrics data for run: {run_id}")
    process_run(run_id)
    print("\nDone!")
