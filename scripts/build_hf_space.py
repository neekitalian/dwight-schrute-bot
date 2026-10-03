"""Stage an allowlisted, committed research Space; never copy private run data.

Run from any directory: python scripts/build_hf_space.py --output build/hf-space
This command performs no network operations. The destination must not exist.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "LICENSE", "NOTICE", "pyproject.toml", "docs/VWAP-LICENSE",
    "docs/architecture.md", "docs/workflow.md", "docs/vwap.md",
    "docs/validation.md", "docs/huggingface.md",
    "docs/connections.md", "docs/data-providers.md", "dwight/connection_catalog.py", "dwight/connection_checks.py",
    "dwight/databento_data.py", "dwight/massive_data.py", "dwight/vendor_history.py", "dwight/private_config.py",
    "docs/manual-paper.md", "docs/manual-observer.md", "docs/manual-milestones.md", "docs/walkforward.md", "docs/finrl-research.md",
    "docs/real-data.md", "docs/enrichment.md", "docs/quickstart.md", "docs/tradingview-alerts.md", "docs/toolkit-release.md",
    "docs/experiments-and-reports.md", "docs/server-experiment.md", "docs/transformer-research.md",
    "dwight/__init__.py", "dwight/__main__.py", "dwight/runner.py",
    "dwight/store.py", "dwight/research.py", "dwight/data.py",
    "dwight/experiments.py", "dwight/ops.py", "dwight/shadow.py",
    "dwight/firstrate.py", "dwight/enrichment.py", "dwight/toolkit.py", "dwight/tradingview.py",
    "dwight/walkforward.py", "dwight/manual.py", "dwight/manual_reporting.py", "dwight/context.py", "dwight/finrl.py",
    "dwight/signals.py", "dwight/manual_signals.py", "dwight/manual_campaign.py",
    "dwight/paper.py", "dwight/recorder.py", "dwight/connectors/__init__.py",
    "dwight/audit.py", "dwight/reporting.py", "dwight/campaign.py",
    "dwight/connectors/csv.py", "dwight/connectors/polymarket.py",
    "vwap_bot/__init__.py", "vwap_bot/__main__.py", "vwap_bot/engine.py",
    "examples/make_experiment_demo.py", "deploy/huggingface/app.py",
    "deploy/huggingface/research.py", "deploy/huggingface/analytics.py",
    "deploy/huggingface/charts.py", "deploy/huggingface/presentation.py",
    "deploy/huggingface/transformer_analysis.py",
    "deploy/huggingface/walkforward_view.py", "deploy/huggingface/starter.py",
    "deploy/huggingface/platforms.py",
)
MAPPED_FILES = {
    "README.md": "PROJECT-README.md",
    "deploy/huggingface/README.md": "README.md",
    "deploy/huggingface/entrypoint.py": "app.py",
    "deploy/huggingface/requirements.txt": "requirements.txt",
}


def stage(output: Path, root: Path = ROOT) -> dict:
    root = root.resolve()
    if output.exists():
        raise ValueError("destination already exists; choose a new deployment directory")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    # Read the committed blob, not the working-tree file. Local edits/secrets can
    # never hitch a ride in a deployment carrying an older revision's identity.
    blobs = {}
    for source, destination in [(p, p) for p in FILES] + list(MAPPED_FILES.items()):
        mode = subprocess.check_output(["git", "ls-tree", revision, "--", source], cwd=root, text=True)
        if not mode.startswith("100644 blob ") and not mode.startswith("100755 blob "):
            raise ValueError(f"missing regular committed source: {source}")
        blobs[destination] = subprocess.check_output(["git", "show", f"{revision}:{source}"], cwd=root)
    manifest = {
        "schema_version": 1,
        "source_repository": "https://github.com/neekitalian/dwight-schrute-bot",
        "source_commit": revision,
        "application": "synthetic_research_and_public_data_checks",
        "broker_execution_enabled": False,
        "files": {p: hashlib.sha256(data).hexdigest() for p, data in sorted(blobs.items())},
    }
    output.mkdir(parents=True, exist_ok=False)
    for destination, raw in blobs.items():
        target = output / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    (output / "source-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = stage(args.output)
    print(json.dumps({"directory": str(args.output.resolve()),
                      "source_commit": manifest["source_commit"],
                      "files": len(manifest["files"]) + 1}, indent=2))


if __name__ == "__main__":
    main()
