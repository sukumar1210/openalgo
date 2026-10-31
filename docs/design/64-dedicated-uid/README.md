# Running the OpenAlgo container as its own uid (fork, proposal)

**Status: proposed, for the operator's decision.** Nothing here is built. It changes who owns production's state, so
it is decided on this page before any code. The implementation would be one fork change, one algo-trading change and
one window with a `sudo` step, each described below.

## Why

A Python strategy runs with `RLIMIT_NPROC` set to `STRATEGY_NPROC_LIMIT`, a guard against a runaway strategy forking
without end. Linux counts that limit against **every process and thread of the strategy's real uid on the whole
machine**, not against the strategy's own.

The container runs as `appuser`, uid 1000, which is also the host's own user. Everything that user runs counts against
every strategy's limit:
- the tick recorder and its collectors;
- research jobs;
- Claude Code sessions and their MCP servers;
- the Actions runner and cron.

On 2026-10-28 the limit was 256 and the uid ran about 282 threads, so both hosted strategies died at their 09:16
decision with `can't start new thread`. Nothing alerted; a person found it. The fix that shipped (fork `75c0c47f8`,
`STRATEGY_NPROC_LIMIT = '4096'`) is headroom, not isolation: the count still rises with whatever the host user runs
(295 on 2026-10-31). A dedicated uid for the container removes the coupling, so the limit counts only OpenAlgo's own
threads.

## The design: change the uid, keep the group

| | Today | Proposed |
| --- | --- | --- |
| Container user | `appuser`, uid 1000, gid 1000 | `appuser`, **uid 1501**, gid 1000 |
| State owner (`/srv/data/openalgo`) | 1000:1000 | **1501**:1000 |
| State modes | as created (`.env` 600, `keys/` 700, the rest 755/644) | group read and write throughout, directories setgid; `.env` 660, `keys/` 2770 with 660 files |
| New files the container creates | umask 022 (644) | umask 002 (664), group 1000 |

The group stays 1000 on purpose. `RLIMIT_NPROC` counts the real uid only, never the group, so a shared group couples
nothing. Keeping it gives access to the three parties that need the state without a new group, a `usermod` or a
re-login:
- **the host user** (uid 1000, primary group 1000): cron, the paper watchdog, the backup, the runner;
- **the pipeline's rollback image**: every image built before this change runs as uid 1000, gid 1000;
- **the new container**, as the owner.

uid 1501 is unused on this host. A matching host account (`openalgo`, no login) is optional; it only makes `ls`
print a name.

## What it touches

**The fork:**
- **`Dockerfile`:**
  - `appuser` is pinned to uid and gid 1000 for upstream's installers, which chown the host `.env` to 1000
    (marketcalls/openalgo#1394).
  - Add `ARG APP_UID=1000` and create `appuser` with `--uid ${APP_UID} --gid 1000`. The default build is identical
    to today, so upstream installs and upstream merges are unaffected.
- **`start.sh`:**
  - When `/app` is writable it runs `chmod -R 755 db log strategies` and `chmod 700 keys`. On the bind-mounted state
    that strips the group bits this design needs.
  - When the uid is not 1000, it sets `umask 002` and uses `g+rwX` and setgid directories instead (`keys/` 2770).
  - Unchanged for uid 1000.

**algo-trading:**
- **`integrations/openalgo/setup.sh`** (line 31, `docker build -t "$TAG" .`): add `--build-arg APP_UID=1501`.
- **`integrations/openalgo/docker-run.sh`:**
  - No `--user` is passed; the image's own user applies. No change to the run.
  - Its first-install branch chowns `.env` to `1000:1000` with mode 600. That becomes `1501:1000` with mode 660.
- **`docker-run.sh`, just before `docker run`:** run `chmod -R g+rwX` and `find -type d -exec chmod g+s` on the
  state directory, so the forward step needs no hands. Every start goes through this script: `make oa-restart`,
  `make oa-restart-only` and the pipeline's restart.
  - It runs as the host user, who owns every file a uid-1000 image wrote, and every file uid 1501 wrote is already
    group-writable.
  - It is idempotent and cheap.

**What reads and writes the state, and why each keeps working:**

| Who | What it does | After the change |
| --- | --- | --- |
| Strategies, gunicorn, the websocket proxy, MCP servers started by `docker exec` | write db, log, tmp, paper_data, strategies | owner, uid 1501. These are the container's own threads, which the limit should count |
| `make deploy` / `tools/deploy_release.py` | `docker exec ... pip install` into `/app/.venv` | runs as the image's user (1501), and `/app/.venv` is `COPY --chown=appuser`, so it is owned by 1501 |
| `paper_watchdog` (cron, uid 1000) | reads `paper_audit/`; reads `settings.analyze_mode` from `db/openalgo.db` | group read. SQLite's `-shm` needs write, which is group write |
| `refresh_results_calendar` (cron, uid 1000) | writes `paper_data/results_calendar.csv` | group write on the setgid directory. Its 644 file is read, never written, by the container |
| The release health check | reads the container's uid via `docker inspect` and `/proc/<pid>/status` | follows the uid change by itself (research-1) |
| `deploy_queue.backup` (runner, uid 1000) | stages db, strategies and `.env`, then `tar -c` | group read. The pipeline never restores state itself; a manual restore uses `tar -xp` under the setgid directories, so the files stay group-writable |
| `make oa-move-state` | renames entries within one filesystem | group write on the directories |
| homelab backup (user timer, uid 1000) | stages with SQLite's online backup, then restic | group read, and group write for `-shm` |
| Research scripts | open `historify.duckdb` read-only | group read |
| The Actions runner | `docker` group, `live.json`, oa-* targets | unchanged |

**Rolling back to an image from before the change** needs no step. That image runs as uid 1000, gid 1000 and reaches
everything through the group. Files it creates are 644 and owned by 1000; the next forward deploy's
`docker-run.sh` step (host user, their owner) makes them group-writable again before uid 1501 starts.

## Migration

Do this in a market-closed window, with the pipeline held and no collector running.
1. **Merge the fork change.** The default build is uid 1000, so it deploys like any other change, with no behaviour
   change.
2. **Merge the algo-trading change:** the build argument, the first-install branch and the permissions step in
   `docker-run.sh`.
3. **In the window:**
   1. `make queue-hold REASON="dedicated uid"`.
   2. Check that no `collect@*` unit is active. `oa-down` and both restarts refuse while one runs (algo-trading #28).
   3. `make oa-down`.
   4. As the host user, while still the owner:
      - `chmod -R g+rwX /srv/data/openalgo`;
      - `find /srv/data/openalgo -type d -exec chmod g+s {} +`;
      - `chmod 660 /srv/data/openalgo/.env`;
      - `chmod 2770 /srv/data/openalgo/keys`.
   5. **The `sudo` step, the operator's:** `sudo chown -R 1501 /srv/data/openalgo`. The owner only; the group stays
      1000.
   6. `make openalgo`, which builds with `APP_UID=1501`, then `make oa-restart`. That restarts the container and
      redeploys the installed release; `oa-restart-only` would leave `/app/.venv` without it.
   7. Health, then the planted check below.
   8. `make queue-unhold`.
4. **Record the change:** CLAUDE.local.md and the fork-setup notes say the state is owned by 1501:1000.

## Rollback

Deploy the previous image. Nothing else is required, because the group gives it full access. To return the ownership
too: `sudo chown -R 1000 /srv/data/openalgo`.

## The planted check

It proves the limit counts only the container's threads, and runs after step 3.6.
1. **The uids:** `docker exec openalgo id -u` prints 1501, and `stat -c %u /srv/data/openalgo/db/openalgo.db` prints
   1501.
2. **Host threads do not count:**
   - `N = $(ps -L -u 1501 --no-headers | wc -l) + 20`.
   - As the host user, hold 300 extra threads: `python3 -c "import threading,time; [threading.Thread(target=time.sleep, args=(120,), daemon=True).start() for _ in range(300)]; time.sleep(120)" &`.
   - Then: `docker exec openalgo /app/.venv/bin/python -c "import resource,threading; resource.setrlimit(resource.RLIMIT_NPROC,($N,$N)); threading.Thread(target=lambda: None).start(); print('started')"` prints `started`.
3. **Container threads do count:**
   - Hold 50 threads inside the container: `docker exec -d openalgo /app/.venv/bin/python -c "<the same 50-thread holder>"`.
   - Repeat the command from check 2: it now fails with `can't start new thread`.

Before the change, check 2 fails too: on 2026-10-28 that is what happened, with 256 in place of N.

## Not covered

- **The 4096 limit:** `STRATEGY_NPROC_LIMIT` stays. With a dedicated uid it bounds OpenAlgo's own threads, which is
  what it was meant to do.
- **Docker user-namespace remapping** (`userns-remap`) would also separate the uids. It is daemon-wide, it remaps
  every container and bind mount, and it changes every file's numeric owner. This proposal does the same job for one
  container with one build argument.
