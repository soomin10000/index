# ~/projects

This directory is the **parent index repo** (`soomin10000/index`, public) containing
several **independent sibling repos**, each with its own `.git` and its own GitHub
remote. It is not one big repo — treat each sibling as a separate project.

Current siblings (gitignored here, listed at the bottom of `.gitignore`):
`How_hot_is_harold`, `bad_parents`, `bbc_spoofer`, `next_train`, `now_playing`,
`watched_pot`, `weather`, `whats_darren_doing`.

`homelab_mcp/` and `hcpy/` also have their own `.git` but are **not yet** in the
sibling gitignore block — `hcpy` is a vendored third-party tool (separately
ignored), but `homelab_mcp` is a real gap: a bare `git add -A` at the parent level
will try to add it as a broken embedded-repo gitlink. Add sibling dirs to
`.gitignore` as they're created, and never run `git add -A` / `git add .` here —
stage files explicitly, and commit each repo from inside its own directory.

## Pushing

- Parent repo: SSH remote `git@github-soomin:soomin10000/index.git` — this alias
  authenticates correctly, use it as-is.
- Sibling repos: their remotes are plain `git@github.com`/`https` and do **not**
  authenticate with the `github-soomin` alias. Push over HTTPS instead:
  `gh auth setup-git` once per machine, then push normally (or
  `git push https://github.com/soomin10000/<repo>.git HEAD:<branch>`).
- `gh repo create --source=. --remote=origin` sets a plain SSH remote, which hits
  the same auth failure — immediately run
  `git remote set-url origin https://github.com/<owner>/<repo>.git` after creating
  a new sibling repo this way.

## This repo is public

`soomin10000/index` is a **public** GitHub repo. Never hardcode secrets, API keys,
or tokens as defaults in tracked source — use `os.environ.get('X_SECRET')` with no
default (raise/exit if unset). Real secret values live in the crontab environment
or in `*.service` files, which are gitignored (`*.service` is blanket-ignored;
`home_menu/home-menu.service` is the sole tracked exception because it carries no
secret). `wireless-lab/` (crackable wifi captures) must never be published either.
