# Run-directory Data Exchange Design

## Goal and scope

Separate the loader's in-memory state from the caller and geometry tools. The caller continues to invoke `Agent_functions` methods directly, while both consume the loader's persisted JSON files. Calibre execution remains disabled.

## Data flow

`FAGENT.__main__` treats the existing `--output` value as the parent output directory and creates a `rundir` subdirectory beneath it. It runs the loader with that directory as its output directory, then starts the caller with the run-directory path and the existing configuration. No loader instance is passed to the caller or functions.

The loader writes `<output>/rundir/gds.json` and `<output>/rundir/drc.json`. The caller reads both JSON files from this directory to build the model payload. It creates `Agent_functions` with the same run-directory path. The functions block reads the saved JSON: `gds.json` supplies the original GDS path via its `source` field for `gdstk`; `drc.json` is the default normalized DRC input. Original GDS remains necessary for lossless geometry editing and is never overwritten. The caller may invoke function methods directly and maintains the bounded tool-calling loop.

The run directory also contains `candidate.gds` when saved, and `conversation.json` and `result.json` after a caller run. `--load-only` stops after writing loader JSON. A model response or a baseline DRC report cannot establish a fresh DRC pass.

## Interfaces and errors

`Agent_caller` and `Agent_functions` accept a run-directory path rather than a loader instance. Neither imports `Agent_loader`; main remains responsible for invoking loader and caller in sequence. Missing files, invalid JSON, missing `gds.json` source, and missing original GDS fail with clear errors. The loader retains its existing source/output overwrite safeguards and output schema. Existing source protection must apply to all run-directory outputs.

`Agent_functions.read_drc_log` consumes normalized JSON instead of parsing raw Calibre input; parser ownership remains with the loader. Existing raw-report parsing methods in functions are removed rather than retaining an import of the loader.

## Verification

Use temporary run directories and real loader outputs to verify JSON-only handoff, geometry read and save, model tool feedback, and output paths. Exercise missing and malformed JSON, missing GDS source, `--load-only`, and preservation of original input bytes. Run the full pytest suite. Mock only model/provider communication; do not claim Calibre validation or a real model run from tests.
