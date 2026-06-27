# Pytest Tool Reliability Design

## Goal

Fix two observed `PytestTool` failures without changing its public parameters or
its 20-second execution timeout:

1. Test paths containing spaces must reach pytest as one argument.
2. Failed runs must expose pytest's complete short traceback to the agent.

## Command Construction

`PytestTool` continues to pass a command string to `Runtime.exec`. It first builds
an argument list containing Python, pytest, the test path, fixed pytest flags,
and parsed extra arguments. It then serializes that list for the host shell:

- Windows uses `subprocess.list2cmdline`.
- POSIX uses `shlex.join`.

Extra arguments are parsed with `shlex.split` so quoted expressions such as
`-k "foo and bar"` remain a single pytest argument. Invalid quoting produces a
failed `ToolResult` with a clear argument-parsing error instead of executing a
malformed command.

This is intentionally scoped to `PytestTool`; changing the Runtime interface to
accept argv would affect every runtime and tool implementation.

## Failure Output

Successful runs keep the existing compact statistics-only output. Failed runs
return pytest's raw output generated with `--tb=short`, including assertion and
exception details.

Output at or below `MAX_OUTPUT_CHARS` is returned unchanged apart from outer
whitespace. Longer output retains both its beginning and end, separated by an
explicit truncation marker. The final output never exceeds the configured cap.
Keeping both ends preserves early failure context and pytest's final summary.

Timeout detection and the `PYTEST_TIMEOUT = 20` limit remain unchanged.

## Tests

Regression coverage will verify:

- An absolute test path containing spaces executes successfully.
- A failed assertion exposes its exception type and custom message.
- Oversized failed output contains the truncation marker, retains head and tail
  context, and respects `MAX_OUTPUT_CHARS`.
- Existing `PytestTool` and runtime tests continue to pass.

## Non-Goals

- Changing `Runtime.exec` to accept argument arrays.
- Changing the timeout or adding per-test timeout plugins.
- Parsing pytest output into a new structured schema.
