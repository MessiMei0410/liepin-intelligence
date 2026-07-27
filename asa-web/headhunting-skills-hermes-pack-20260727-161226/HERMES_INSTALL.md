# Hermes-readable Install Notes

Purpose: install a curated set of headhunting/recruiting skills into the local Hermes/Codex skill directory.

Target directory:

```text
~/.codex/skills
```

Installation rule:

1. Copy every directory under `skills/` into `~/.codex/skills/`.
2. If a target skill directory already exists, back it up before replacing it.
3. Preserve the original directory names exactly.
4. Do not rename `SKILL.md`.

Recommended command for the human operator:

```bash
./install-for-hermes.sh
```

Post-install verification:

```bash
ls ~/.codex/skills/headhunting-workflow/SKILL.md
ls ~/.codex/skills/candidate-matching-report/SKILL.md
ls ~/.codex/skills/headhunt-liepin/SKILL.md
```

Suggested Hermes self-check prompt:

```text
请读取 ~/.codex/skills 中的猎头相关 skill，确认 headhunting-workflow、candidate-matching-report、headhunt-liepin、liepin-job-publish、resume-export 是否可用，并按用途分组列出。
```

Included skills:

- headhunting-workflow
- headhunting-copilot
- headhunting-search-strategy
- recruitment-analysis
- candidate-matching-report
- candidate-salary-report
- salary-negotiation-feedback
- talent-pool
- headhunt-liepin
- liepin-cdp-search
- liepin-job-publish
- resume-export
- resume-local-save
- resume-docx-export
- multi-channel-search
- headhunt-note-generator

Notes for Hermes:

- These are local skills, not remote agents.
- Load the relevant `SKILL.md` completely before using a skill.
- If multiple skills apply, prefer the narrowest skill that matches the task.
- For end-to-end recruiting work, start from `headhunting-workflow`.
- For Liepin-specific browser/search/publish work, prefer `headhunt-liepin`, `liepin-cdp-search`, or `liepin-job-publish`.
- For candidate/JD evaluation, prefer `candidate-matching-report`.
