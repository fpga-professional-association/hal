# HAL — FPGA Professional Association headless fork

This is FPGAPA's headless fork of [emsec/hal](https://github.com/emsec/hal). The Qt GUI has been removed by design: HAL is driven by the `hal` CLI, the `hal_py` Python bindings, scripts, and AI agents. Do not reintroduce GUI code or Qt dependencies — the codebase is fully Qt-free.

Project skills live in `.claude/skills/` — start with `using-hal` for how to drive the tool, generate visuals (`tools/hal_viz`), and handle interactive waveforms (Saleae Logic 2 MCP). The assistant-agnostic, canonical skill set is `ai/skills/` (per-tool SKILL.md files: hal_viz, hal_agilex, capabilities/findings, bitstream, the analysis tools, the container bench, and the RE walkthrough method); when a `.claude` copy and `ai/skills` disagree, `ai/skills` wins.

## Working rules

### CI/CD failure escalation
If a CI/CD build or test run fails **more than twice** for the same piece of work (i.e., on the third consecutive failure), STOP autonomous fix attempts. Fable (the planning model in the main session) must review before anyone continues:

1. Subagents and workflows must not push further fix attempts or dispatch new CI runs; they return to the main session instead.
2. The escalation must include: what failed in each run (with the relevant log excerpts), what was changed between attempts and why, and the current hypothesis.
3. Fable reviews the pattern, decides the approach (different fix, revert, or defer to a human), and only then may execution resume.

Rationale: repeated failed CI cycles are expensive (~30–60 min per Linux build) and usually mean the diagnosis is wrong, not the patch.

### Division of labor
Fable plans, writes issues, and reviews; implementation work is delegated to Opus agents (complex/core changes) and Sonnet agents (mechanical/docs changes), per FPGAPA convention.

## Building and testing
HAL builds on Linux/macOS only (see README / `install_dependencies.sh`). There is no local Windows build — verification runs through GitHub Actions on this fork (`ubuntu22.04`, `ubuntu24.04` and `arm64` support `workflow_dispatch`; `ubuntu26.04`, `macOS` and `releaseDoc` run on push/PR only). CI is the compile-and-test authority; source-level consistency checks are the local substitute.
