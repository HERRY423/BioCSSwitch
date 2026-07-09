from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "packs" / "bio-singlecell" / "singlecell_server.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("bio_singlecell_server", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


MODULE = _load_module()


def test_snakemake_workflow_quotes_shell_paths_and_string_params():
    recipe = MODULE.sc_workflow_recipe(
        engine="snakemake",
        input_h5ad="data/my input.h5ad",
        include_scfm=False,
    )
    snakefile = recipe["files"]["Snakefile"]
    assert "--input {input:q}" in snakefile
    assert "--output {output:q}" in snakefile
    assert "--batch-key {params.batch_key:q}" in snakefile
    assert "--metrics {output.metrics:q}" in snakefile
    assert "--input-h5ad {params.input_h5ad:q}" in snakefile


def test_workflow_recipe_defaults_to_not_runnable_until_scfm_is_configured():
    recipe = MODULE.sc_workflow_recipe(engine="snakemake")
    assert recipe["runnable"] is False
    assert recipe["artifact_type"] == "workflow_package"
    assert recipe["configuration_required"]


def test_workflow_recipe_is_runnable_when_no_followup_configuration_is_needed():
    recipe = MODULE.sc_workflow_recipe(
        engine="snakemake",
        include_scfm=True,
        scfm_model_dir="models/geneformer",
    )
    assert recipe["runnable"] is True
    assert recipe["artifact_type"] == "runnable_workflow"
    assert recipe["configuration_required"] == []
