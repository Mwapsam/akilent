# Manual deploy (when GitHub Actions is down)

Normally every green CI run on `main` deploys itself (`.github/workflows/deploy.yml`). When
GitHub Actions is down or stuck, `tools/deploy/manual_deploy.sh` releases from your machine in
the same way: database snapshot, build, migrate, collectstatic, restart, nginx reload, health
check, automatic code rollback if `/healthz` never answers, then CloudFront and Cloudflare.

## One-time setup

1. Copy `tools/deploy/manual_deploy.env.example` to `tools/deploy/manual_deploy.env`
   (gitignored) and fill in the SSH details. They are the GitHub secrets `SSH_HOST`,
   `SSH_USER`, `SSH_PORT` and `DEPLOY_PATH`.
2. Make sure your SSH key can log in: `ssh -p <port> <user>@<host> 'cd <path> && git log -1 --oneline'`.
3. Optional: `GITHUB_REPO` + `GITHUB_TOKEN` (read access to Actions) so the script can confirm CI
   passed; `CLOUDFLARE_ZONE_ID` + `CLOUDFLARE_API_TOKEN` to purge the landing page.

## Deploying

```bash
tools/deploy/manual_deploy.sh --dry-run        # see the plan first: commit, what's live, CI state
tools/deploy/manual_deploy.sh                  # deploy origin/main (asks you to type "deploy")
```

- **CI gate.** Like GitHub, it only ships a commit that passed CI. With GitHub's API reachable
  and a token set it checks the CI run. If it can't, either test locally with `--run-tests`
  (check out the commit first) or accept the risk with `--skip-ci-check`.
- **GitHub fully down.** If `git fetch` fails, the script deploys your local `main` and sends the
  commit to the server as a git bundle over SSH (`--bundle` forces this).
- **Rolling back.** `tools/deploy/manual_deploy.sh --sha <good-commit> --skip-ci-check`. The plan
  says ROLLBACK. Migrations are not reversed. Each deploy's database snapshot is on the server in
  `backups/pre-deploy/`.
- **Afterwards.** Run [smoke-test.md](smoke-test.md).

## What's different from a GitHub deploy

- **`.env` is not rebuilt.** GitHub writes the server's `.env` from its secrets on every deploy.
  The manual script keeps the `.env` the last GitHub deploy wrote and only updates `DEPLOY_SHA`.
  If the release needs a secret that was added since, the script warns with the key names. Add
  them to `<DEPLOY_PATH>/.env` by hand, then re-run. The next GitHub deploy rewrites `.env` from
  secrets as usual, so add the same values to GitHub too.
- **CloudFront** is invalidated from the server's web container (boto3 + the `.env` AWS keys), so
  you don't need the AWS CLI.
- **Only one deploy runs at a time.** The server holds a lock (`.deploy.lock`). If GitHub Actions
  comes back mid-way, its deploy fails on the lock rather than racing this one.

## Keeping it in step

`tools/deploy/remote_release.sh` repeats the release steps from `deploy.yml`.
`apps/core/tests/test_manual_deploy.py` fails if `deploy.yml`'s release commands change
without it.
