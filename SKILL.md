---
name: vogsync
description: Use vogsync to configure, diagnose, commit, and deploy 42 projects from a single Git main repo to separate Vogsphere repositories. Load for vogsync commands, setup, troubleshooting, or deployment safety.
---

# Use vogsync safely

Use this skill when helping with vogsync workflows. Prefer the installed CLI's `--help` and the repository's `README.md` for details that may change; do not invent commands or treat the old `render` command as supported.

## Mental model

- The **main repo** is one Git repository, usually backed up to GitHub. Each project is a real, top-level directory in it, not a nested Git repository or submodule. Run vogsync from anywhere inside the main repo; when at its root, name the project explicitly for project-scoped commands.
- `.vogsync.json` at the main repo root maps project names to **separate Vogsphere URLs**. The current Git branch's configured upstream is the main repo destination; it is not a project's Vogsphere URL. Never point a project URL at the main repo: deployment force-pushes to that URL.
- `git subtree split` extracts only committed history under the project directory. Project files land at the Vogsphere repository root, without the parent directory. `deploy` does not stage or commit changes.

## Choose the operation

1. Identify the Git root, current branch/upstream, intended top-level project, and destination before proposing a write. Use `vogsync doctor` for read-only, offline setup checks; `vogsync status [PROJECT]` for local status; and `vogsync remote list` for configured project URLs. Use `vogsync doctor --remote` for remote read-access checks, not proof of push access. `vogsync status PROJECT --remote` compares the project split to Vogsphere `master`, regardless of the branch selected for a previous deploy.
2. For setup, use `vogsync remote set PROJECT URL` only with the user's actual Vogsphere URL. Review `.vogsync.json` before committing it to a **private** main repo; it is not ignored by default. Its optional `exclude` list contains exact top-level non-project directory names, not paths or glob patterns. Do not use `remote set` for the main repo's Git upstream.
3. For local work, use `vogsync add PROJECT` to stage new, modified, and deleted project files; inspect with `vogsync diff PROJECT` or `vogsync diff PROJECT --cached`. Use `vogsync commit PROJECT -m 'Message'` for a project-prefixed commit. Without `-a`, it commits **all staged files**, including files outside the named project: inspect the entire index first. With `-a`, it includes tracked modifications/deletions in that project only; it does not add untracked files. `--push` pushes the main repo commit to its configured upstream, **not** to Vogsphere.
4. For deployment, verify the project URL and target branch with the user. Require committed project files, a clean working tree, and a current branch with an upstream. Run `vogsync deploy PROJECT --dry-run` to check the split and Vogsphere read connectivity; it pushes to neither destination and cannot establish push permissions. Only after the user approves the force-push to that specific destination and branch, run `vogsync deploy PROJECT` (default Vogsphere branch: `master`) or `vogsync deploy PROJECT --branch BRANCH`.

## Deployment consequences

- A real `deploy` first pushes the **whole main repo** to its upstream without force, then **force-pushes** the project split to the configured Vogsphere URL/branch. These pushes are not atomic. If the main repo push fails, Vogsphere is untouched; if the Vogsphere push fails, the main repo has already been pushed. Report which destination changed and resolve the failure before retrying.
- `--branch` changes only the Vogsphere branch, not the main repo's upstream branch. Do not assume it matches the local branch or `master`.
- `--pull` runs `git pull --ff-only` before splitting; even with `--dry-run`, it can update the local branch. Do not add `--pull` as an innocuous check.
- `vogsync restore PROJECT` discards unstaged project changes. `vogsync restore PROJECT --staged` only unstages them. Never suggest the former as a harmless cleanup step.

## Quick reference

```sh
vogsync doctor                         # Offline, read-only diagnosis
vogsync status PROJECT                 # Local project status
vogsync remote list                    # Review Vogsphere URLs
vogsync add PROJECT                    # Stage this project's files
vogsync diff PROJECT --cached          # Inspect staged project changes
vogsync commit PROJECT -m 'Message'    # Commit (inspect all staged files first)
vogsync deploy PROJECT --dry-run       # Split and check remote read access; no push
vogsync deploy PROJECT                 # After approval: push main repo, then force-push project
```

For installation, platform-specific commands, and more examples, consult the repo's `README.md`. On Windows use the appropriate Python launcher and shell quoting; do not assume POSIX shell syntax works in Command Prompt.
