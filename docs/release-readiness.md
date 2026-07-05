# BioCSSwitch Release Readiness Gates

This file turns the six release risks into auditable gates. A BioCSSwitch
release should not be tagged until each gate has a dated artifact or an
explicit waiver.

## Gate 1: Pack Manifest And Dependency Graph

Run:

```bash
python3 test/test_bio_offline.py
```

Evidence:

- `test_pack_manifests` parses every `packs/*/pack.json`.
- Every `depends_on` target must exist.
- Dependency graph must be acyclic.
- Every server script and Skill directory referenced by a manifest must exist.
- `desktop/src-tauri/src/packs.rs` expands enabled packs to their dependency
  closure before writing MCP config.

## Gate 2: Version Consistency

Run:

```bash
python3 test/test_bio_offline.py
```

Evidence checked offline:

- `desktop/package.json` version equals `desktop/src-tauri/Cargo.toml`.
- `desktop/package-lock.json` root version equals `desktop/package.json`.
- `bio-audit-grade` server version equals `packs/bio-audit/pack.json`.

## Gate 3: Local Build And Toolchain Evidence

Run:

```bash
bash scripts/release-verify.sh
```

For a release candidate:

```bash
bash scripts/release-verify.sh --build
```

Expected evidence:

- Python offline tests pass.
- `cargo test` passes in `desktop/src-tauri`.
- If `--build` is used, `npm ci` and `npm run tauri build` pass.
- Missing Cargo/Node/npm is a release blocker, not a soft warning.

## Gate 4: Real Claude Science Canary

Run from the app:

1. Open the "MCP / Skill path verification" panel.
2. Click "start verification" to write the canary MCP and canary Skill.
3. Restart/open the sandboxed Science session.
4. Trigger the canary MCP and confirm the marker is observed.
5. Trigger the canary Skill and confirm `canary-ok`.
6. Clean up the canary.

Save a small evidence JSON and validate it:

```bash
python3 scripts/check-canary-evidence.py path/to/canary-evidence.json
```

Minimal evidence shape:

```json
{
  "schema": "csswitch/canary-evidence/1",
  "checked_at": "2026-07-05T00:00:00Z",
  "science_version": "recorded from the installed app",
  "marker": "the canary marker prefix is enough",
  "mcp": {"status": "passed", "note": "marker observed"},
  "skill": {"status": "passed", "note": "canary-ok observed"}
}
```

## Gate 5: Provider Matrix

Run at least two providers with at least three repeats:

```bash
python3 test/bio_eval/run.py --proxy http://127.0.0.1:18991/<secret> --label deepseek --repeat 3
python3 test/bio_eval/run.py --proxy http://127.0.0.1:18992/<secret> --label qwen --repeat 3
python3 test/bio_eval/run.py --matrix
python3 test/bio_eval/provider_matrix_gate.py --min-providers 2 --min-repeat 3
```

Required evidence: overall score, red-team score, tool-call score, stability
stdev, and estimated cost.

## Gate 6: Expert Gold Calibration

Run:

```bash
python3 test/bio_eval/gold_calibration.py --check
```

For release readiness:

```bash
python3 test/bio_eval/gold_calibration.py --check --strict
```

`--check` verifies that every case with a `rubric.gold` entry appears in
`test/bio_eval/gold_calibration.json`. `--strict` additionally requires each
entry to be `approved`.
