from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "packs" / "bio-workflows" / "generators" / "sc_scanpy_pipeline.py"


def _generate(tmp_path: Path, *extra: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    output = tmp_path / "generated_pipeline.py"
    command = [
        sys.executable,
        str(GENERATOR),
        "--h5ad",
        "data/input.h5ad",
        "--out",
        str(output),
        "--outdir",
        "results",
        *extra,
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    return result, output


def test_generator_emits_stage_gated_pipeline(tmp_path: Path):
    result, output = _generate(
        tmp_path,
        "--sample-key",
        "donor_id",
        "--condition-key",
        "condition",
        "--batch-key",
        "library_batch",
        "--doublet-key",
        "capture_id",
        "--include-doublet",
    )
    assert result.returncode == 0, result.stderr
    source = output.read_text(encoding="utf-8")
    compile(source, str(output), "exec")

    required_contracts = (
        "count_like",
        "design_audit.json",
        "qc_thresholds_by_group.tsv",
        "doublet_summary_by_group.tsv",
        "X_pca_unintegrated",
        "integration_audit.json",
        "cluster_seed_stability.tsv",
        "cluster_resolution_sensitivity.tsv",
        "cluster_sample_support.tsv",
        "analysis_readiness.json",
        "pseudobulk_condition_de_ready",
    )
    assert all(contract in source for contract in required_contracts)
    assert "condition-level differential expression" in source
    assert "scrub_doublets" in source
    assert "harmony_integrate" in source

    manifest = json.loads(
        output.with_suffix(".py.manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["recipe_hash"].startswith("sha256:")
    assert {"scrublet", "harmonypy"} <= set(manifest["required_packages"])
    assert manifest["params"]["sample_key"] == "donor_id"


def test_generator_requires_sample_for_condition(tmp_path: Path):
    result, output = _generate(tmp_path, "--condition-key", "condition")
    assert result.returncode != 0
    assert not output.exists()
    assert "--condition-key requires --sample-key" in result.stderr


def test_generator_without_optional_methods_still_compiles(tmp_path: Path):
    result, output = _generate(tmp_path)
    assert result.returncode == 0, result.stderr
    source = output.read_text(encoding="utf-8")
    compile(source, str(output), "exec")
    assert "scrub_doublets" not in source
    assert "harmony_integrate" not in source
