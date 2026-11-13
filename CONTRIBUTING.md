# Contributing

Preserve the low-cognitive-load interface: compact results first, exact advanced
schemas through discovery, no unsolicited traces/images and no invented certainty.
Keep behavior changes focused and explain physical effects, preconditions, cleanup,
partial failures and hardware coverage. Do not add arbitrary SCPI execution.

Run the README's automated tests on Linux. Add meaningful failure/limit tests for
new behavior. Simulator results must remain labeled. Real-hardware claims need
model/firmware, wiring, known source, actual settings, outcome and restoration
evidence. Protect private captures and redact identity/configuration before sharing.
Do not relax the supported-model check without model-specific validation.

Separate source/test success from physical verification. Never declare calibration,
RF compliance or all-format support from one fixture run. Follow existing artifact
limits and retention; do not silently return huge arrays or retry controls.

Dependencies are not vendored. Check applicable licenses before adding third-party
code or assets. Contributions should be offered under the project's MIT license
and be code/documentation you have the right to contribute. No CLA is currently set.
Public support and compatibility commitments will follow actual maintainer capacity.
