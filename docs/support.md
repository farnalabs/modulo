# Get support

Modulo runs on your infrastructure. Nobody at Farnalabs can see your instance
unless you deliberately send us something. This page tells you what to send
when something breaks, what the file contains, and how to check it before you
send it.

---

## 1. When something breaks

Start with the single command for your install shape. Each one writes one zip
file that you can open, read, and then send.

### Native install (Linux)

```bash
modulo support
```

- Writes `./modulo-support-<ref>.zip` in the current directory, where `<ref>`
  is the bundle's per-run reference.
- Prints the reference line, the bundle path and its size, and any member it
  could not collect, listed as `missing <member>: <reason>`.
- If something looked like it might carry a credential, the bundle leaves it
  out rather than shipping it, and names it in a `withheld:` line:
  `withheld: <member> — <n> item(s) removed (<reason>)`. A whole small member
  that cannot be split is withheld entirely, as
  `withheld: <member> — member removed (<reason>)`.
- Pass `--out <path>` to write the zip somewhere else.

`modulo doctor --report report.zip` also writes a redacted diagnostic zip, to a
path you choose. It exits with the doctor status, not the report status: `0`
healthy, `1` unhealthy, `2` degraded (warnings), `3` uninitialized. A non-zero
exit does not mean the report was not written. Look for the
`diagnostic report written: ...` line and check that the file exists.
`modulo doctor` also prints its checks to the terminal while it builds the
report, so you can read the summary before opening the zip.

If the install itself failed, there is no `modulo` binary to run yet. The
installer prints a delimited support block as its final output on failure and
keeps the full transcript under `~/.local/state/modulo/install-*.log`; send
both the block and the log file. Both the transcript and the block are landing
in this milestone. Until they are in your build, capture the installer output
yourself:

```bash
bash scripts/install.sh 2>&1 | tee install.log
```

### Docker Compose

```bash
bash scripts/support-bundle.sh
```

- Run it from the repository root on the host that runs the stack.
- Writes `modulo-support-bundle-<UTC>.zip` in the repository root by default.
  Pass `--out <path>` to choose a destination.
- Works with the stack down: it still collects the Docker-level facts and
  records the app-level part as missing in `MANIFEST.txt`. It does not fail the
  whole run because a container is unhealthy.
- Needs `bash`, `docker` with the Compose plugin, and `zip`.
- Other options: `--compose-file <file>`, `--project-name <name>`,
  `--app-service <name>` (default `backend`), `--tail <n>` and `--no-app`.
  `./scripts/support-bundle.sh --help` lists them all.

`modulo doctor --report` is launcher-scoped and does not apply to a Compose
deployment. If you also want the raw logs as separate files, capture them
before bringing the stack down:

```bash
docker compose ps -a > compose-ps.txt
docker compose logs --no-color --tail=400 > compose-logs.txt
```

### Kubernetes / Helm

You do not need to install anything. Capture the cluster facts and ask the app
pod to build the bundle, then hand the files to whoever operates the cluster.

```bash
NAMESPACE=modulo   # namespace the release is installed in
RELEASE=modulo     # helm release name

# 1. What is running, and what is failing?
kubectl -n "$NAMESPACE" get pods -l app.kubernetes.io/instance="$RELEASE" -o wide

# 2. Raw logs for each component. The bundle collapses repeats; these do not.
kubectl -n "$NAMESPACE" logs -l app.kubernetes.io/component=backend --tail=400 --prefix > backend.log
kubectl -n "$NAMESPACE" logs -l app.kubernetes.io/component=saq-runner --tail=400 --prefix > saq-runner.log
kubectl -n "$NAMESPACE" logs -l app.kubernetes.io/component=saq-system --tail=400 --prefix > saq-system.log

# 3. Build the bundle inside the backend pod and copy it out.
POD=$(kubectl -n "$NAMESPACE" get pod -l app.kubernetes.io/component=backend -o jsonpath='{.items[0].metadata.name}')
kubectl -n "$NAMESPACE" exec "$POD" -- python -m modulo.support.bundle --out /tmp/modulo-support.zip
kubectl -n "$NAMESPACE" cp "$POD":/tmp/modulo-support.zip ./modulo-support.zip
```

Send `modulo-support.zip` and the three log files to `support@modulo.run`. The
labels above come from the chart in `deploy/helm/modulo/`; if your release uses
different names, read them from the pod labels
(`kubectl -n "$NAMESPACE" get pods --show-labels`).

If you also want the pod description as a separate file:

```bash
kubectl -n "$NAMESPACE" describe pods -l app.kubernetes.io/instance="$RELEASE" > pods.txt
```

---

## 2. The one-line reference

Before you build anything, the product usually already printed a line worth
pasting into a ticket. These are the exact shapes.

**A failed run** (copy it from the run page):

```text
run 3f0c1a2e-6d7b-4f6a-8c1e-2b9d5a7f0e31 · pipeline 0b6e4c1d-9a2f-4d3b-8e5c-1f7a2d9b6c40 (Nightly release) · status failed · error provider.unavailable
url <the address you use to reach Modulo>/runs/3f0c1a2e-6d7b-4f6a-8c1e-2b9d5a7f0e31
```

The `url` line is the origin your browser is using, so the line works without
you knowing a configured public base URL.

**An error group** (copy it from the Error Dashboard):

```text
error 7c1d2e3f-4a5b-6c7d-8e9f-0a1b2c3d4e5f · fingerprint 9f8e7d6c5b4a · level error · count 42 · source backend
```

**A failed install** (printed as the installer's last output):

```text
----- Modulo install failed: support reference 4f2a91c0 -----
  Install target   : bundle-v0.3.324 (linux-x86_64)
  Failing stage    : verify
  Install log      : /home/you/.local/state/modulo/install-20261102T101500Z-4f2a91c0.log
  Support reference: 4f2a91c0
----- end of Modulo install support block -----
```

**A bundle** (printed by `modulo support` and by the bundle module when the zip
is written):

```text
modulo support bundle 1 ref=MSB-1-20261102T101500Z-deadbeef
bundle: /home/you/modulo-support-MSB-1-20261102T101500Z-deadbeef.zip (348201 bytes)
Open the bundle and check it before sending it to Farnalabs support.
```

If a member could not be collected, a `missing <member>: <reason>` line follows
the summary. If the redaction gate removed something because it looked like it
might carry a credential, a `withheld: <member> — <n> item(s) removed
(<reason>)` line appears there too: the bundle still builds, minus that named
item.

The run, error-group and installer lines are landing in this milestone.
On an older build, `modulo doctor --report` prints
`diagnostic report written: <path>` and does not print the reference.

Send the line before you build anything. Between them the lines usually answer
the first questions: which version and install shape you are on, which
component failed, and which run failed. Build the zip only when the line is not
enough.

---

## 3. What is in the bundle

The bundle is a zip with a fixed, versioned member list. A member cannot appear
without a format-version change, so the list below is the contract.

| Member | What it carries | Install shape |
|---|---|---|
| `MANIFEST.txt` | Format version, reference, every member with its byte count and what it carries, redaction substitution counts, a `withheld:` list for anything the redaction gate removed, and a `missing:` list for anything that could not be collected. | All |
| `report.json` | The same inventory, machine-readable: format version, members and bytes, per-layer redaction counts, missing sources, and `withheld` records (`member`, `items`, `reason`, `scope`). | All |
| `version.txt` | Modulo version, build commit, Python version, OS, architecture, UTC offset and clock drift. | All |
| `doctor.txt` | Output of the launcher doctor checks. | Native (recorded as missing in a container) |
| `health.json` | Health check names, statuses and error codes only. No check detail strings. | All |
| `settings.txt` | Full settings **key inventory**. Values only for a reviewed safe-list (`MODULO_DB`, the public and frontend URLs, ports, worker pool and concurrency counts), plus the plan tier and feature flags. Every other key is shown as `set` or `unset`. | All |
| `deployment.txt` | Install shape, service names, image tags and digests, restart counts, proxy variable names, CA bundle path. | All |
| `db.txt` | Database migration head, expected head, Postgres version, and row counts for a fixed table list. No rows. | All |
| `errors.json` | Top error groups: id, fingerprint, level, source, count, first and last seen, environment, version, sanitized message. | All |
| `runs.json` | Last failed runs: run id, pipeline, status, error code, timings, cost. No inputs, outputs or node standard output. | All |
| `licence.txt` | Plan tier, license state, expiry, opaque license id, feature list. Never the license key. | All |
| `degraded.json` | Terminal-degrade reason and the last crash backtraces, redacted. | Native launcher only |
| `logs/<component>.log.tail` | Bounded log tail per component. Consecutive repeated lines are collapsed, so a crash loop cannot fill the bundle. | Native (recorded as missing in a container; Docker Compose carries `docker/logs/...` instead) |
| `docker/...` | `compose ps -a`, container state, health, restart count and exit code per service, image tags and digests, compose file names and hashes, volume names only, bounded per-service logs. | Docker Compose |
| `host/...` | OS, kernel, architecture, Docker client and server versions, Compose version, UTC time and clock drift. | Docker Compose |

Log tails and error messages are free text. They are the most useful part of
the bundle and the part most likely to contain something you did not expect.
Read them.

---

## 4. What is deliberately not in it

The collector does not collect or copy any of the following:

- Credentials: API keys, tokens, passwords, private keys, webhook signing
  secrets.
- `secrets.json` and `.env`.
- Pipeline graphs, agent prompts and node configuration.
- Run inputs and outputs, node standard output, connector request and response
  bodies.
- Connector configuration.
- User identities: the user list, email addresses, SSO provider configuration.
- Any other organization's data.

The bundle is built to exclude credentials and customer content. It is plain
text - open it and read it before you send it.

Redaction runs in layers and fails closed. If something still looks like it
carries a secret after redaction, the collector removes it before writing the
zip and names it in a `withheld:` line, rather than shipping it. If the
offending text cannot be isolated, the build refuses to write the zip, names
the member, and exits non-zero. That is a guard against known secret shapes,
not a guarantee about arbitrary free text. A log line can contain anything,
which is why you should read it first.

---

## 5. How to check before sending

1. Open the zip with any archive tool. Read `MANIFEST.txt` first: it lists every
   member, its size, and what it carries, in plain text. It also names anything
   that was withheld, and why.
2. Look at the short members first: `version.txt`, `deployment.txt`,
   `settings.txt` and `db.txt` are a few dozen lines each. The log tails are the
   long ones.
3. If the Modulo package is available where you are, `--list` and `--show`
   inspect a bundle without unpacking it. In a Compose install the app
   container already has it:

   ```bash
   docker compose exec -T backend python -m modulo.support.bundle --out /tmp/modulo-support.zip
   docker compose exec -T backend python -m modulo.support.bundle --out /tmp/modulo-support.zip --list
   docker compose exec -T backend python -m modulo.support.bundle --out /tmp/modulo-support.zip --show settings.txt
   ```

   The first command builds an app-only bundle inside the container (Docker
   members come only from the script in section 1). The next two read it.
   `--list` prints members and sizes; `--show <member>` prints one member. Both
   refuse to print a member that would fail the redaction gate.

4. Delete any member you are not comfortable sharing, then send the rest to
   `support@modulo.run`. `MANIFEST.txt` still lists the members that were
   generated, and names anything that was withheld so you can see exactly what
   was left out and why. Say which ones you removed when you send it.

---

## 6. What we do with it

Nothing in Modulo uploads a bundle automatically. There is no Farnalabs
endpoint, no phone-home and no upload step: every member is built locally by
the command you ran and written to the path you chose. Until you send it, the
bundle lives only on the machine that produced it.

**What we do with it.** Nothing uploads automatically. When you send us a
bundle we keep it in a dedicated support folder and use it only to diagnose
your report. We delete it when the issue is closed, and never later than 7 days
after that - or immediately, if you ask. We keep a short written summary of the
issue (version, install shape, error, cause, fix) that contains none of your
data. We will not run your bundle through a model, a pipeline or any third
party without asking you first.

Send it to `support@modulo.run`.

The collector makes no model calls and sends nothing to any Farnalabs service.
Whoever you send the bundle to reads it as a plain archive; no part of the
collection path sends it to a model or to a pipeline.

---

## 7. If you are not the operator

If you use Modulo but do not run the instance, forward this page - or the
reference line you copied - to whoever operates it. The commands need access to
the host (native or Compose) or the cluster (Kubernetes), so the operator is
usually the only person who can run them. Send the bundle only to that operator
or to `support@modulo.run`, never to anyone who asks for it unprompted.

---

## Related

- [Troubleshooting](./troubleshooting.md) - symptoms, causes and resolutions.
- [Deployment guide](./deployment.md) - Compose and Kubernetes deployment.
- [Upgrade process](./upgrade-process.md) - upgrading an existing instance.

This page is also published at
[modulo.run/docs/support](https://modulo.run/docs/support). Where the two
differ, this page wins: it ships with the version you are running.
