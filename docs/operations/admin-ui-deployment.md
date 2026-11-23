# Publish the GBrain companion dashboard on macOS

The dashboard application lives in a separate repository. Make application
changes there, merge them into its `main` branch, and pin the resulting full
Git commit in `scripts/admin-ui-revision` in this repository. Review the pin
change on `master` before running the deployment on the Mac that hosts it.

The host needs the existing GBrain runtime and launchd jobs, a local checkout
of the dashboard source with read access to `origin`, and Python 3.11 with the
tarfile data filter available. The Git credential for the source checkout is
provided by the host's credential helper. The deploy script uses the installed
Bun runtime and the pinned dependency lockfile to check and build TypeScript
releases. Older dashboard pins with prebuilt JavaScript use Node.js for their
syntax check.

Run the preflight from the GBrain deployment checkout:

```sh
python3 scripts/deploy-admin-ui.py \
  --ui-repo /path/to/dashboard-checkout \
  --host-root /path/to/host
```

To activate that exact version, run the command as the macOS user who owns the
launchd jobs and add `--apply --origin https://dashboard.example.invalid:8443`.
Use the actual Tailscale origin for that host. The script checks that the pin is
on the dashboard repository's remote `main`, verifies runtime compatibility,
and stages and tests a clean copy of the app. It then switches the two dashboard
jobs and verifies the three frontend assets, host status, Source listing, and
read-only model route status through Tailscale. A failed verification restores
the previous application and restarts both jobs.

On the first activation, the original `admin-ui` directory moves to
`.admin-ui-host` on the same disk. Its OAuth file, logs, and reader state remain
available through the stable `admin-ui` path. The active directory's
`.deployed-revision` records the complete dashboard commit. To roll back a
later successful deployment, revert the release pin on `master` and deploy it
again.
