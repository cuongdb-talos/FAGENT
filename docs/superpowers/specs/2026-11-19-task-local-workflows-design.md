# Task-local workflows and execution trace

## Goal

Support DRC, LVS, and new tasks such as `migrate_gds` using a shared graph framework. Adding or changing a task's workflow steps or agent tools should require changes within that task's directory, plus setup configuration when selecting the task. Shared environment configuration remains common to all tasks.

## Organization and loading

Each task resides in `FAGENT/tasks/<task>/` with a `workflow.yaml`, step and tool implementations, and task-specific prompts and schemas. Shared engine, model, checkpoint, and environment services remain outside task directories. The existing setup YAML selects a task and provides run-specific inputs; the existing shared configuration supplies model and EDA environment settings. A task name resolves to its task directory without a central task registry.

For example, a setup entry `migrate_gds` with `input_gds: input/cell.gds` resolves to `FAGENT/tasks/migrate_gds/workflow.yaml`. A workflow step may reference `steps:read_gds`, and an agent step may list `tools:inspect_layer`. References resolve to callable Python functions within that task's package. Descriptions and parameter schemas for agent tools are declared alongside their implementation, so they do not need a separate registry or duplicated JSON definition. A shared agent step constructs model requests from the task's prompt, response schema, and declared tools.

The loader checks task identifiers and confines module resolution to the selected task package. Before execution it validates the workflow, unique step identifiers, references, callable interfaces, tool metadata, dependencies, and routing targets. Errors identify the task and offending step or tool. Loading task Python code executes trusted local project code; path confinement is not a sandbox.

## Execution and state

The engine constructs and runs a graph from the declared steps and routing using shared execution code, without a task-specific action allowlist or hard-coded task sequence. Workflow steps receive a consistent context containing run inputs, shared services, and prior state, and return serializable updates. Tool functions use a consistent callable interface and declare their model-visible parameters. Exact interfaces will be specified in the implementation plan after inspecting existing call sites.

Agent tool calls are validated against their declared schema before invocation. The engine records the tool response in the conversation and follows the agent loop. A failed step or tool is reported with its identity and cannot be treated as successful validation; recovery and continuation must follow explicit workflow behavior rather than silently reporting success.

Preserve checkpoint identity, review interrupts, existing DRC candidate handling, validation evidence and storage-consent safeguards while migrating DRC and LVS to task-local definitions. Do not imply that simulated validation is a real DRC or LVS pass. Runtime objects such as loaded models and GDS libraries remain in services rather than serialized graph state. Keep the existing task behavior unless the new extension interface requires an explicitly documented change.

## Verbose trace

Add a `--verbose` CLI option. With it enabled, display the task, actual graph node, workflow step status, invoked tool names, and the actual next node. Do not dump input or output data. Report errors with task and step or tool identity regardless of verbose mode. Statuses are `running`, `completed`, `failed`, and `waiting` for an interrupt.

Illustrative output:

```text
# [task] migrate_gds
## [node] read -> migrate:
##### read: completed
## [node] migrate -> validate:
##### migrate: running
#### [tools] inspect_layer:
#### [tools] change_layer:
##### migrate: completed
## [node] validate -> END:
##### validate: completed
```

Events must appear as they happen. A conditional next node is not known until routing has completed: display the node initially with its next destination pending, then display the selected destination after routing. Do not predict the next node or delay tool events to manufacture the illustrative ordering. Include repeated visits and tool calls in execution order; indicate failure or waiting without claiming a successful next step.

## Verification

Exercise DRC and LVS workflows against existing behavior and safeguards, including simulated validation and review. Add a representative `migrate_gds`-style test task to demonstrate task discovery, step and tool resolution, and execution without editing the engine. Test invalid paths, missing functions, duplicate steps, invalid tool arguments, failure handling, and trace events for successful, conditional, failed, and waiting runs. Confirm that verbose output does not expose input/output payloads and that normal operation does not emit verbose trace.

## Non-goals

Implementing a production GDS migration algorithm or adding new real EDA backends is outside this change. A test task may use small representative functions to demonstrate the extension mechanism.
