# 9:00 PushPlus Send Retry Implementation Plan

## Goal

Make the 9:00 scheduled summary resilient to transient PushPlus failures while preserving a failed task result when no delivery is confirmed. The existing 9:28 monitoring task is out of scope.

## Scope

- Modify `scheduled_ashare_workflow.py` only for the scheduled summary send path.
- Add regression coverage in `tests/test_scheduled_ashare_workflow.py`.
- Use the existing scheduled workflow log as the persistent failure log.

## Design

- Attempt the PushPlus request at most three times.
- Wait five seconds between failed attempts.
- Log every failed attempt with its attempt number and error text.
- If all attempts fail, log the terminal failure and re-raise the workflow error so Task Scheduler records a failed run.
- Write the sent-state JSON only after PushPlus accepts the message.

## Verification

- Test a transient failure followed by success: two calls, one five-second delay, sent state written.
- Test all attempts failing: three calls, two delays, failure is logged, no sent state is written.
- Run the complete scheduled-workflow test module and the full test suite.
