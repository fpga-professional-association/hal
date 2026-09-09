# ai/skills — how to drive this repo's tools well

Skill files for AI agents (and humans in a hurry) working on FPGAPA's headless
HAL fork. Each `SKILL.md` is self-contained: when to reach for the tool, the
commands that actually work, and the pitfalls that have actually bitten agents
in this repo — nothing speculative.

This directory is the **canonical, assistant-agnostic** home. Assistant-specific
directories (`.claude/skills/`, `.agents/skills/`) may carry loader copies or
pointers; when they disagree, this directory wins and they need a sync.

| skill | reach for it when |
| --- | --- |
| [using-hal-python](using-hal-python/SKILL.md) | loading, traversing or modifying a netlist through `hal_py` or `hal --python-script` |
| [container-build-and-test](container-build-and-test/SKILL.md) | anything needing a built HAL — there is no Windows build; the `halbuild` container is the bench |
| [hal-viz](hal-viz/SKILL.md) | rendering module trees, gate graphs, dataflow groups, clock trees, or findings reports |
| [hal-agilex](hal-agilex/SKILL.md) | Quartus `.vo` exports: importing, primitive coverage, reference simulation, recognition |
| [hal-capabilities-and-findings](hal-capabilities-and-findings/SKILL.md) | discovering what plugins can do, validating capability declarations, emitting findings documents |
| [hal-bitstream](hal-bitstream/SKILL.md) | starting from a device bitstream instead of a netlist |
| [netlist-analysis-tools](netlist-analysis-tools/SKILL.md) | CDC screening, FSM recovery, semantic diffing, and the other `tools/hal_*` analyses |
| [re-walkthrough-method](re-walkthrough-method/SKILL.md) | reverse-engineering an unknown netlist — the method distilled from `examples/agilex3_walkthroughs` |

Conventions all skills follow:

- Commands are copy-pasteable from the repository root, container-ready where
  HAL is needed (`HAL_BASE_PATH`, `HAL_PY_PATH`, `PYTHONPATH` spelled out).
- Pitfalls sections list real failures with the real fix, not hypotheticals.
- Every claim about a tool's behaviour is checkable against that tool's README
  or tests; if you find a skill contradicting the tool, the tool is right —
  fix the skill.
