---
name: smart-commit
description: Trigger when the user says "commit", "save changes", or runs "/commit".
---

# Smart Commit Workflow

When triggered, follow this workflow:

1. **Pre-flight check**: Run `git status --porcelain`. If empty, tell the user there are no changes to commit and stop.
2. **Stage changes**: Run `git add .`, then inspect `git diff --staged`.
3. **Generate a commit message** from the staged diff, in this format:

   ```text
   type(scope): short subject

   - bullet of what changed
   - bullet of why
   ```

4. **Commit**: Run `git commit -m "<generated message>"`. Briefly report success.

## Commit message rules

- Types: `feat`, `fix`, `refactor`, `chore`, `docs`, `style`, or `test`.
- Keep scope relevant to the architecture (for example `ui`, `api`, `deps`, `auth`, or `seo`).
- Keep the imperative subject under 60 characters (for example, `add eSewa QR`).
- Include body bullets when changes span multiple files or involve complex logic.
- Never include a `Co-Authored-By` trailer.

## Tool adaptation

Use the current environment's command execution tool to run the Git commands. Do not assume a tool named `run_command` exists; in Codex, use the available shell command tool. Never print credentials or secret environment variables.
