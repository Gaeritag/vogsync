---
name: vogsync
description: Use vogsync to configure, diagnose, commit, and deploy 42 projects from a single Git main repo to separate Vogsphere repositories. Load for vogsync commands, setup, troubleshooting, or deployment safety.
---

# Use vogsync safely

Use this skill when helping with vogsync workflows. Prefer the installed CLI's `--help` and the repository's `README.md` for details that may change; do not invent commands or treat the old `render` command as supported.

## Mental model

- The **main repo** is one Git repository, usually backed up to GitHub. Projects are real top-level directories or properly registered top-level group submodules. `.gitmodules` alone stores group paths/clone URLs; `.vogsync.json` stores only deployment URLs/exclusions. Accidental nested repositories and nested submodules remain unsupported. Run vogsync from the main repo or inside a group; `.git` files are supported. At the root, name projects explicitly.
- `.vogsync.json` at the main repo root maps project names to **separate Vogsphere URLs**. The current Git branch's configured upstream is the main repo destination; it is not a project's Vogsphere URL. Never point a project URL at the main repo: deployment force-pushes to that URL.
- Regular projects use `git subtree split`; groups deploy their own committed history directly. Project files land at the Vogsphere repository root, without the parent directory. `deploy` does not stage or commit changes.

## Choose the operation

1. Identify the Git root, current branch/upstream, intended top-level project, and destination before proposing a write. Use `vogsync doctor` for read-only, offline setup checks; `vogsync status [PROJECT]` for local status; and `vogsync remote list` for configured project URLs. Use `vogsync doctor --remote` for remote read-access checks, not proof of push access. `vogsync status PROJECT --remote` compares the project split to Vogsphere `master`, regardless of the branch selected for a previous deploy.
2. For setup, use `vogsync remote set PROJECT URL` only with the user's actual Vogsphere URL. Review `.vogsync.json` before committing it to a **private** main repo; it is not ignored by default. Its optional `exclude` list contains exact top-level non-project directory names, not paths or glob patterns. Do not use `remote set` for the main repo's Git upstream.
3. For local work, use `vogsync add PROJECT` to stage new, modified, and deleted project files; inspect with `vogsync diff PROJECT` or `vogsync diff PROJECT --cached`. Regular `commit` creates a project-prefixed parent commit; without `-a`, it includes **all parent staged files**, even outside the project. Regular `-a` limits tracked modifications/deletions to the project. Group commands route inside the group: without `-a`, commit its index; with `-a`, include tracked modifications/deletions, never untracked files. Group messages are unchanged. See the two-repository workflow below. `--push` never deploys to Vogsphere.
4. For deployment, verify the project URL and target branch with the user. Require committed project files, a clean working tree, and a current branch with an upstream. Run `vogsync deploy PROJECT --dry-run` to check the split and Vogsphere read connectivity; it pushes to neither destination and cannot establish push permissions. Only after the user approves the force-push to that specific destination and branch, run `vogsync deploy PROJECT` (default Vogsphere branch: `master`) or `vogsync deploy PROJECT --branch BRANCH`.

## Deployment consequences

- A real regular `deploy` pushes the **whole main repo** without force, then **force-pushes** the project split to Vogsphere. Group deployment first pushes the group normally, then the parent normally, then force-pushes the group's selected commit **only to Vogsphere**. Stop on failure and report destinations already changed. These pushes are not atomic; never force-push collaboration repositories or roll back successful commits automatically.
- `--branch` changes only the Vogsphere branch, not the main repo's upstream branch. Do not assume it matches the local branch or `master`.
- For regular projects, `--pull` runs `git pull --ff-only` before splitting; even with `--dry-run`, it can update the local branch. Groups reject `--pull`, even in dry runs: update/reconcile both repositories explicitly, select a group branch, review and pin HEAD. Group dry runs never push, commit, initialize, select branches or update pointers.
- `vogsync restore PROJECT` discards unstaged project changes. `vogsync restore PROJECT --staged` only unstages them. Never suggest the former as a harmless cleanup step.

## Group setup, publishing and recovery

- Clone with `git clone --recurse-submodules URL`, or explicitly initialize using `git submodule update --init -- GROUP`. Initialization often detaches HEAD: explicitly run `git -C GROUP switch BRANCH` (or create a tracking branch). Never initialize, fetch or switch branches just to run offline diagnostics. Both repositories need read access; publishing needs push access and branch upstreams. Group pointer commits need Git 2.36+.
- A new group must be registered with `git submodule add URL GROUP` and its `.gitmodules`/gitlink committed first. Never derive the Vogsphere destination from the clone URL. Require the user's separate Vogsphere URL and explicit authorization for real force-push deployment.
- `vogsync commit GROUP -m 'fix: sorting'` preflights both repositories, commits inside the group, then automatically makes a **pointer-only** parent commit (`chore(GROUP): update group repository revision`). Unrelated parent staged/unstaged work is preserved. A staged group pointer that would be overwritten blocks the operation: inspect before explicitly unstaging. Parent hooks run against an isolated index and may not add unrelated entries.
- `--no-pin` commits only the group. `vogsync pin GROUP` records its current HEAD after direct Git commits or partial failure, never pushes, and is a no-op if already recorded. It accepts a staged pointer already equal to group HEAD. It can record unpublished commits: warn that pushing the parent alone can reference unavailable history.
- `commit --push` preflights upstreams before committing, pushes the group FIRST and parent ONLY after success, both normally. `--no-pin --push` pushes only the group and leaves the parent pointer unrecorded. Without `--push`, both commits remain local.
- On a failed parent commit, preserve the successful group commit; inspect `vogsync status GROUP`, fix the cause and run `vogsync pin GROUP`. On failed group push, do not push the parent. On failed parent push, explain the group is already published; reconcile explicitly then retry the parent push. Never reset, delete, pull, merge or rebase automatically.
- Group `deploy` requires initialized, clean repositories, group and parent branches/upstreams, and both parent index/committed pointers matching group HEAD. Detached group HEAD is rejected for commit/pin/push/deploy; tell the user how to select a branch. Status/doctor distinguish internal changes from parent pointer changes and remain offline unless `--remote` is requested.

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
