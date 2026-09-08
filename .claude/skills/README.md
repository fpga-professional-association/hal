# Claude Code Project Skills

This directory holds Claude Code project skills for the FPGAPA HAL fork.

## Layout

Each skill lives in its own subdirectory as `<skill-name>/SKILL.md`:

```
.claude/skills/
  my-skill/
    SKILL.md
```

A `SKILL.md` starts with YAML frontmatter containing `name` and `description`,
followed by the markdown instructions Claude should follow:

```markdown
---
name: my-skill
description: What this skill does and when Claude should use it.
---

Instructions go here.
```

Skills placed here are auto-discovered by Claude Code for anyone working in
this repository, so they are shared with the whole team via git.

## Available skills

- [`using-hal`](using-hal/SKILL.md) — how to drive headless HAL: building,
  the `hal` CLI, the `hal_py` Python bindings, the surviving plugins, and how
  to generate human-viewable visualizations with `tools/hal_viz`.
