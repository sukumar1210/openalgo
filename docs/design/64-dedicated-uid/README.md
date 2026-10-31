# Running the OpenAlgo container as its own uid (fork, proposal)

**Status: proposed, revised after review (2026-10-31), for the operator's decision.** Nothing here is built. It
changes which uid production's processes run as, so it is decided on this page before any code. The implementation
would be one fork change, one algo-trading change and one market-closed restart. Unlike the first draft, there is no
`sudo chown` of the state.

## Why

A Python strategy runs with `RLIMIT_NPROC` set to `STRATEGY_NPROC_LIMIT`, a guard against a runaway strategy forking
without end. Linux counts that limit per (user namespace, real uid): against **every process and thread of the
strategy's real uid on the whole machine**, not against the strategy's own.

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

**It works on this host as described.** The container runs in the initial user namespace: `/proc/<pid>/uid_map` is
`0 0 4294967295`, `HostConfig.UsernsMode` is empty, there is no `/etc/docker/daemon.json`, and `docker info` lists no
userns or rootless option. So uid 1501 inside the container is host uid 1501, which nothing on the host uses
(`getent passwd 1501` is empty). Processes started with `docker exec` run as 1501 and count against the limit, which
is intended: MCP servers, the deploy's pip and the health probes are OpenAlgo's.

## The design: change the process uid, keep every file's owner

| | Today | Proposed |
| --- | --- | --- |
| Container user | `appuser`, uid 1000, gid 1000 | `appuser`, **uid 1501**, gid 1000 |
| Owner of existing state (`/srv/data/openalgo`) | 1000:1000 | **unchanged, 1000:1000** |
| Owner of files the container creates from now on | 1000:1000 | 1501:1000 (group from the setgid directories) |
| State modes | as created (`.env` 600, `keys/` 700, the rest 755/644) | group read and write throughout, directories setgid: `.env` 660, `keys/` 2770 with 660 files |
| New files the container creates | umask 022 (644), some 0600 | 664 and 2775, through every writer (see "Keeping the state group-writable") |

**Why the owner stays 1000.** The first draft chowned the state to 1501. The review showed that makes rollback
need `sudo`: a uid-1501 owner can clear the group bits, and only root can give the files back. Keeping the owner at
1000 means:
- **Rollback needs no step for the files that exist today.** The uid-1000 image is still their owner.
- **A restored node is right by construction.** `deploy/setup_node.sh --restore` extracts as uid 1000.
- **The container cannot loosen or tighten the files it does not own.** Its chmods on 1000-owned files fail, and
  every such call already catches the failure: `start.sh:183` (`|| echo`), `python_strategy.py:1167`
  (`except Exception: pass`), `utils/oauth_keys.py:46` and `:148` (log a warning).
- **No `sudo chown -R`,** and no window in which the state belongs to nobody who can repair it.

**Why the group stays 1000.** `RLIMIT_NPROC` counts the real uid only, never the group, so a shared group couples
nothing. Group 1000 has one member, the host user (`getent group 1000` is `sukumar:x:1000:`), so group access gives
no one access they do not already have. Keeping it gives three parties the state without a new group, a `usermod`
or a re-login:
- **the host user** (uid 1000): cron, the watchdog, the backups, the runner and the tools below;
- **the pipeline's rollback image**: every image built before this change runs as uid 1000, gid 1000;
- **the new container**, uid 1501, through the group.

What remains is files the container creates from now on, owned by 1501. They must be group-writable, from every
writer, always. That is the real work in this proposal, and the next section takes each writer in turn.

**A reserved account.** `sudo useradd --system --uid 1501 --gid 1000 --no-create-home --shell /usr/sbin/nologin
openalgo` keeps 1501 from being handed to a future user, and makes `ls` print a name. It is the only `sudo` step.

## Keeping the state group-writable

Every file the container creates must be group-readable and group-writable, and every directory group-writable and
setgid. A file that is not breaks three things at once: the host's backups, the host tools that read it, and the
rollback image. umask alone does not get there:
- `tempfile.mkstemp` creates 0600 whatever the umask;
- `docker exec` processes get runc's default umask, 022;
- code that tightens modes on purpose sets 0600 or 0700.

### The fork

**One switch and one helper.** The image sets `ENV OPENALGO_GROUP_SHARED_STATE=1` when it is built with an
`APP_UID` other than 1000. A small `utils/state_modes.py` reads it once and exports the modes every writer uses:
`PRIVATE_FILE_MODE` (0600, or 0660 when shared), `PRIVATE_DIR_MODE` (0700, or 2770), `SHARED_FILE_MODE` (0664) and
`share(path)`, which applies the file mode after a `mkstemp`. Unset, every mode is today's, so upstream installs and
upstream merges are unaffected.

| Writer | Today | With the switch |
| --- | --- | --- |
| `Dockerfile` | `appuser` uid and gid pinned to 1000 for upstream's installers (marketcalls/openalgo#1394) | `ARG APP_UID=1000`, `--uid ${APP_UID} --gid 1000`; `ENV OPENALGO_GROUP_SHARED_STATE` set only when `APP_UID` is not 1000. The default build is identical to today |
| `start.sh:183-185` | `chmod -R 755 db log strategies`, `chmod 700 keys` | `umask 002`; `chmod -R g+rwX` and setgid directories on what it owns; `keys/` 2770. Unchanged without the switch |
| `blueprints/python_strategy.py:1167` | `os.chmod(log dir, 0o755)` on every start, clearing group write and setgid | adds the owner's bits (`mode | 0o700`), never clears the group's |
| `utils/oauth_keys.py` | `keys/` re-tightened to 0700, key files to 0600 | `PRIVATE_DIR_MODE` and `PRIVATE_FILE_MODE` |
| `services/agent/chatgpt_oauth.py` (`_harden`) | `db/chatgpt_oauth` 0700, its auth file 0600 | the same two constants |
| `services/openscript_store.py:181`, `blueprints/openscript_runner.py:395` | `mkstemp` then rename: 0600 | `share()` before the rename |
| `blueprints/mcp_http.py:641` (the MCP audit file) | `mkstemp`: 0600 | `share()` before the rename |
| Runs' per-source state, `strategies/state/<id>/` | created under the process umask | inherits `umask 002` from `start.sh`, setgid from its parent |
| The /system permissions page | expects 600/700/644/755, and its fix button resets to them | expects the shared modes when the switch is set, so it neither flags every path nor undoes them |

`mkstemp` calls that write outside the state (`whatsapp_bot_service.py:208` and `:576`, in the container's `/tmp`) are
unaffected.

### algo-trading

| Writer | Change |
| --- | --- |
| `integrations/openalgo/setup.sh:31` | `docker build --build-arg APP_UID=1501 --label org.openalgo.app_uid=1501`, and the tag carries it (below) |
| Every `docker exec` into OpenAlgo (`tools/deploy_release.py` pip, `docker-run.sh migrate`, `tools/setup_machine.sh`, MCP servers) | wrapped as `sh -c 'umask 002; exec ...'`, through one helper so a new caller cannot forget it |
| `algo_trading/paper/paper_audit.py:403` (`save_panel`) | `os.chmod(tmp, 0o664)` after `mkstemp`. Every panel on disk today is already `-rw-------` |
| `integrations/openalgo/docker-run.sh` | its first-install branch writes `.env` as 660, not 600. Its permissions step runs `find "$STATE" -user "$(id -u)" ...` (the host user's own files only; chmod(2) needs ownership even when the mode would not change) and **fails the start** if that step fails |
| `tools/openalgo_env_keys.py`, `make-env.sh`, `tools/setup_machine.sh` | write `.env` as 660 with group 1000, never 600 |
| `deploy_queue.backup` and the homelab backup | walk with `os.walk(onerror=...)` that **raises**, so an unreadable directory fails the backup instead of vanishing from it. Today both use `Path.rglob`, which silently skips one (checked on the host's Python 3.13) |

### A standing check, not a one-off

The release health check gains two assertions, run on every deploy and every restart:
1. **The container's uid is 1501.** `capacity_problems()` already reads it from `/proc/<pid>/status`; it fails the
   release when it is anything else.
2. **Nothing in the state is closed to the group.** `find /srv/data/openalgo \( -type f ! -perm -g=rw \) -o
   \( -type d ! -perm -g=rwx \)` must print nothing. A writer that regresses is caught at the next deploy, not at the
   next rollback or backup.

## Image identity carries the uid

The review found that the first draft's migration would not switch the uid at all:
- `setup.sh` skips the build when `openalgo-fork:<short sha>` already exists, and the fork change ships first as a
  uid-1000 image under exactly that tag;
- `make oa-restart` takes `OPENALGO_IMAGE_PIN` from `live.json`, never the image just built;
- `deploy_queue.existing_image` and `ensure_image` find images by fork commit only.

So the uid becomes part of the image's identity:
- **Tag:** `openalgo-fork:<short sha>-u1501`. `existing_image(sha)` and `ensure_image(sha)` look for that tag and
  build it when it is missing. A uid-1000 image of the same commit is never mistaken for it.
- **Label:** `org.openalgo.app_uid=1501`, checked by `ensure_image` before it hands the tag on, so a tag applied by
  hand to the wrong image is refused.
- **`live.json`** records the full tag, so `oa-restart` restarts the uid-1501 image.
- **Rollback** (below) is the one place a uid-1000 image may be chosen, and it says so.

## What reads and writes the state

| Who | What it does | After the change |
| --- | --- | --- |
| Strategies, gunicorn, the websocket proxy, `docker exec` (MCP servers, pip, migrate) | write db, log, tmp, paper_data, strategies | uid 1501, through the group on existing files and as owner of new ones, with the shared modes above. These are the container's own threads, which the limit should count |
| `make deploy` / `tools/deploy_release.py` | `docker exec ... pip install` into `/app/.venv` | runs as 1501 under `umask 002`; `/app/.venv` is `COPY --chown=appuser`, so owned by 1501 |
| `paper_watchdog` (cron, uid 1000) | reads `paper_audit/`; reads `settings.analyze_mode` from `db/openalgo.db` | group read. SQLite gives `-wal`/`-shm` the main file's mode by fchmod, independent of umask, so a group-writable database file is enough |
| `refresh_results_calendar` (cron, uid 1000) | writes `paper_data/results_calendar.csv` | owner of its file; the container only reads it |
| market-recorder's TBT start (`tick_recorder.tbt_decisions_dir`, uid 1000) | reads `paper_audit/*.jsonl` | group read. Today a `PermissionError` there falls into `start_tbt`'s broad except and records the day without the 50-level names, so the group-read check above guards it |
| `make gate`, `make reconstruct`, `make report` (uid 1000) | read the audit files and `paper_panels/*.json` | group read, once `save_panel` writes 664 |
| `tools/whatsapp_group_link.py` (`make alerts-here`, `make alerts-reset`) | **writes** `db/openalgo.db` from the host | owner of the existing file; group write if it is ever recreated by the container |
| `deploy/deploy_window.py` | reads `db/openalgo.db` read-only for the window decision | owner, or group read |
| `algo_trading/paper/sandbox_mode.py`, `integrations/openalgo/login-check.sh`, `tools/status.sh`, `tools/doctor.py` | read the state | owner, or group read |
| `tools/openalgo_env_keys.py`, `tools/setup_machine.sh`, `make-env.sh` | write `.env` | 660, group 1000 (above) |
| `deploy_queue.backup` (runner, uid 1000) | stages db, strategies and `.env`, then `tar -c` | group read; fails loudly on anything unreadable |
| homelab backup (user timer, uid 1000) | SQLite's online backup, then restic | group read, group write for `-shm`; fails loudly on anything unreadable |
| `deploy/setup_node.sh --restore` | `tar -x` as uid 1000, then `rsync` for `keys/` and `paper_data/` | restores as owner 1000, which is this design's ownership; modes come from the archive, which holds the shared modes |
| `make oa-move-state` | renames entries within one filesystem | group write on the directories |
| Research scripts | open `historify.duckdb` read-only | group read |
| The Actions runner | `docker` group, `live.json`, oa-* targets | unchanged; `live.json` now names the `-u1501` tag |

The state's paths are taken from `docker inspect openalgo --format '{{json .Mounts}}'`, not assumed: if
`ALGO_PAPER_DATA` were ever set, `paper_data` would live outside `/srv/data/openalgo`. There are no symlinks under the
state today.

## Migration

In a market-closed window. Every guard is checked, not assumed.
1. **Merge the fork change.** Without the switch it builds and behaves exactly as today, so it deploys like any other
   change.
2. **Hold the pipeline, and confirm it:** `make queue-hold REASON="dedicated uid"`, then `make deploy-status` must
   show the hold before anything else happens. `queue-hold` only dispatches a workflow, so it can return before the
   hold lands. The hold comes before step 4: once the algo-trading change is merged, the pipeline's next fork build
   would otherwise switch the uid outside the window.
3. **Stop the collector timers:** `systemctl --user stop 'collect-*.timer'`, and check that no `collect@*` unit is
   active. `oa-down` refuses while one runs, but once OpenAlgo is down `oa-restart`'s check reports idle without
   looking, and `collect-retry.timer` fires in the evening (18:40 and 19:00 observed).
4. **Merge the algo-trading change** (build argument, tag and label, `docker exec` umask, `save_panel`, `.env`
   writers, scoped permissions step, backups, health assertions).
5. **Open the existing state to the group,** as the host user, its owner, with the container still running (changing
   modes does not disturb it):
   - `chmod -R g+rwX /srv/data/openalgo`;
   - `find /srv/data/openalgo -type d -exec chmod g+s {} +`;
   - `chmod 660 /srv/data/openalgo/.env`;
   - `chmod 2770 /srv/data/openalgo/keys`, and `chmod 660` on the files in it.
   - Then the group check from "A standing check" must print nothing.
6. **Reserve the uid** (the operator's `sudo` step): `sudo useradd --system --uid 1501 --gid 1000 --no-create-home
   --shell /usr/sbin/nologin openalgo`.
7. **Build and restart:** `make openalgo` builds `openalgo-fork:<sha>-u1501`, then `make oa-restart`, which restarts
   the container on that tag and redeploys the installed release (`oa-restart-only` would leave `/app/.venv` without
   it).
8. **Health,** which now includes the uid and group assertions, then the planted check below.
9. **Resume:** `systemctl --user start 'collect-*.timer'`, then `make queue-unhold`.
10. **Record the change:** CLAUDE.local.md and the fork-setup notes say the container runs as 1501, with state owned
    by 1000 and shared through group 1000.

## Rollback

**To a uid-1000 image:** deploy it, naming it explicitly. The pipeline's automatic rollback, and `existing_image`,
never pick a uid-1000 image once `live.json` names a `-u1501` one: going back is a decision, not a fallback. Then:
- **Files that existed before the change** are still owned by 1000. Nothing to do.
- **Files uid 1501 created** are group-writable, because every writer above shares them and the health check has
  refused any that were not. The uid-1000 image reaches them through the group. Nothing to do.
- **If the group check had been failing,** some 1501-owned files are closed to uid 1000, and only root can open them:
  `sudo chown -R 1000 /srv/data/openalgo`, then step 5's chmods. This is the one rollback case that needs a hand, and
  the standing check exists so that it never arises.

**To this design after a rollback:** the uid-1000 image may have created 644 files and 755 directories. Step 5 again
(the host user owns them), then step 7.

## The planted check

It proves the limit counts only the container's threads. It runs once after step 7; the standing guards are the two
health assertions.
1. **The uids:**
   - `docker exec openalgo id -u` prints 1501;
   - a strategy started by the real launcher (its Start button) shows `Uid: 1501 1501 1501 1501` in
     `/proc/<pid>/status`, and `Max processes 4096 4096` in `/proc/<pid>/limits`;
   - `stat -c %u /srv/data/openalgo/db/openalgo.db` still prints 1000.
2. **The control, measured, not assumed:** `ps -L -U 1000 --no-headers | wc -l` (real uid, which is what
   `RLIMIT_NPROC` counts; `-u` would select the effective uid) is recorded and must exceed 200. Under the old uid
   that whole count sat inside every strategy's limit.
3. **Host threads do not count:**
   - `C=$(ps -L -U 1501 --no-headers | wc -l)` and `N=$((C + 20))`.
   - As the host user, hold 300 extra threads: `python3 -c "import threading,time; [threading.Thread(target=time.sleep,
     args=(120,), daemon=True).start() for _ in range(300)]; time.sleep(120)" &`.
   - `docker exec openalgo /app/.venv/bin/python -c "import resource,threading; resource.setrlimit(resource.RLIMIT_NPROC,
     ($N,$N)); threading.Thread(target=lambda: None).start(); print('started')"` prints `started`.
4. **Container threads do count:**
   - Hold 50 threads inside the container: `docker exec -d openalgo /app/.venv/bin/python -c "import threading,time;
     [threading.Thread(target=time.sleep, args=(120,), daemon=True).start() for _ in range(50)]; time.sleep(120)"`.
   - Run check 3's last command again with the same `N`: it now fails with `can't start new thread`.
5. **The same command as uid 1000 fails,** which is what makes 3 meaningful. With the host's 300 threads still held,
   run check 3's last command as `docker exec --user 1000 openalgo ...` with `N=$(( $(ps -L -U 1000 --no-headers |
   wc -l) + 20 ))` measured just before the 300 start: it fails with `can't start new thread`, because the host's
   threads now count. That is the 2026-10-28 failure, reproduced on purpose and without a uid-1000 image.

## Considered and not taken

- **Chowning the state to 1501** (the first draft). It needs `sudo` to roll back, because a uid-1501 owner can close
  files to the group and only root can reopen them, and it makes a restored node wrong by default.
- **Default ACLs** (`setfacl -d -m g::rwX`) on the state directories. They make every writer's umask irrelevant, but
  need the `acl` package on the host, and do not help a file created with an explicit 0600. The writer fixes are
  needed either way, so ACLs would only be a second layer.
- **Docker user-namespace remapping** (`userns-remap`). It is daemon-wide, it remaps every container and bind mount,
  and it changes every file's numeric owner. This proposal does the same job for one container with one build
  argument.

## Not covered

- **The 4096 limit:** `STRATEGY_NPROC_LIMIT` stays. With a dedicated uid it bounds OpenAlgo's own threads, which is
  what it was meant to do.
