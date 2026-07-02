# Secret History Cleanup Design

## Goal

Keep `config/default.yaml` on the local machine while removing it from Git tracking and every published repository revision.

## Scope

- Add `/config/default.yaml` to `.gitignore`.
- Preserve the current local `config/default.yaml` without reading or printing its contents.
- Rewrite all local branches, remote-tracking branches, and tags to remove that path.
- Force-update the GitHub branches and tags after verification.
- Do not alter unrelated files or configuration behavior.

## Procedure

1. Create a recoverable repository backup outside the working tree.
2. Install and use `git-filter-repo`, because it is not currently available locally.
3. Add the exact root-relative ignore rule `/config/default.yaml`.
4. Rewrite all Git references to remove `config/default.yaml` from history.
5. Verify that the local file still exists, is ignored, is not tracked, and is absent from all rewritten commits.
6. Restore the `origin` remote if the rewrite tool removes it, then force-push rewritten branches and tags.
7. Verify the GitHub-facing references after the push.

## Risk And Recovery

History rewriting changes commit IDs. This repository has one user, so no collaborator coordination is required. The pre-rewrite backup is the recovery point if verification fails. No secret rotation is required because the exposed keys are already invalid.

## Success Criteria

- `config/default.yaml` remains available locally.
- Git ignores the file and no branch or tag tracks it.
- No rewritten commit contains the file.
- GitHub `main`, `dev`, and tags point to the cleaned history.
