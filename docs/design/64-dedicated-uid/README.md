# Running the OpenAlgo container as its own uid (fork, proposal)

**Status: proposed, second revision after review (2026-10-31), for the operator's decision.** Nothing here is
built. It changes which uid production's processes run as, so it is decided on this page before any code. The
implementation would be one fork change and one algo-trading change. The switch itself, and any rollback, is then
one commit to `deploy/production.json`, deployed by the pipeline like any other change. There is no `sudo chown`.

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
(`getent passwd 1501` is empty). `Config.User` is `appuser`, which resolves inside the image, so `docker exec`
defaults to the image's uid. No `docker exec -u` appears in algo-trading, market-recorder, homelab or cron. MCP
servers, the deploy's pip and the health probes therefore count against the limit, which is intended: they are
OpenAlgo's.

## The design

**The uid is desired state.** `deploy/production.json` gains `"app_uid": 1501`, beside the fork commit and the
release it already pins. Absent means 1000, which is today. Everything that builds, starts, checks or rolls back
the container reads it from there, so there is one answer to "which uid should be running", and the pipeline
converges to it as it does to a commit.

| | Today | Proposed |
| --- | --- | --- |
| Container user | `appuser`, uid 1000, gid 1000 | `appuser`, **uid `app_uid` (1501)**, gid 1000 |
| Owner of existing state (`/srv/data/openalgo`) | 1000:1000 | **unchanged, 1000:1000** |
| Owner of files the container creates from now on | 1000:1000 | 1501:1000 (1501's primary group is 1000) |
| State modes | as created (`.env` 600, `keys/` 700, the rest 755/644) | group read and write: files 664 (`.env` and key files 660), directories 775 (`keys/` 770) |
| `tmp/` | the container's scratch space (`TMPDIR=/app/tmp`) | **container-private**: excluded from every group check, permissions step and backup walk (see below) |

**Why the owner stays 1000.** A uid-1501 owner could close files to the group, and only root could reopen them, so a
chown would make rollback need `sudo`. Keeping the owner at 1000 means:
- **The files that exist today need nothing on rollback.** The uid-1000 image is still their owner.
- **The container cannot loosen or tighten files it does not own.** Its chmods on 1000-owned files fail, and every
  such call already catches the failure: `start.sh:183` (`|| echo`) and `:185` (`|| true`),
  `python_strategy.py:904` and `:941` (`except: pass`), `utils/oauth_keys.py:46` and `:149` (log a warning),
  `chatgpt_oauth._harden`, and the /system permissions page. The one fatal chmod is in algo-trading's
  `tools/setup_machine.sh:113`, and it changes below.
- **No `sudo chown -R`,** and no window in which the state belongs to nobody who can repair it.

**Why the group stays 1000, and why no setgid.** `RLIMIT_NPROC` counts the real uid only, never the group, so a shared
group couples nothing. Group 1000 has one member, the host user (`getent group 1000` is `sukumar:x:1000:`), so group
access gives no one access they do not already have. 1501's primary group is 1000, and so is the host user's, so
every file either side creates is already group 1000; setgid directories would add nothing. Group 1000 gives the
state to:
- **the host user** (uid 1000): cron, the watchdog, the backups, the runner and the tools below;
- **a uid-1000 image**, on rollback;
- **the container**, uid 1501.

**A reserved account.** `sudo useradd --system --uid 1501 --gid 1000 --no-create-home --shell /usr/sbin/nologin
openalgo` keeps 1501 from being handed to a future user, and makes `ls` print a name. It is the only `sudo` step.

## The pipeline: app_uid end to end

The review found that a uid carried only in the image tag breaks the pipeline: `deploy_queue.live()` resolves the
running commit with `git rev-parse` on the tag's suffix, and for `9ee9ccc4a-u1501` git exits 128 but prints the
argument, so the "running commit" never matches and `decide()` rebuilds, restarts and installs every half hour. And a
hand restart reads `live.json`, which only the pipeline rewrites. So the uid travels as data, not as text in a tag:

| Piece | Change |
| --- | --- |
| `deploy/production.json` | `"app_uid": 1501`. Absent or 1000 is today's behaviour, byte for byte |
| Fork `Dockerfile` | `ARG APP_UID=1000`; `appuser` created `--uid ${APP_UID} --gid 1000`; **`LABEL org.openalgo.app_uid=${APP_UID}` inside the Dockerfile**, so the label certifies what the image was built with, not what the build command asked for. A commit from before this change carries no label and is read as 1000 |
| Image tag | `openalgo-fork:<sha>` when `app_uid` is 1000, as today; `openalgo-fork:<sha>-u<app_uid>` otherwise |
| `live()` | strips a `-u<n>` suffix before `git rev-parse`, and reports `"app_uid"` from the running image's label (`docker inspect --format '{{index .Config.Labels "org.openalgo.app_uid"}}'`, empty read as 1000). A test pins both, including the `9ee9ccc4a-u1501` case |
| `decide()` | a running `app_uid` different from the desired one is a pending change, exactly like a different commit: build, restart, install |
| `existing_image(sha, app_uid)`, `ensure_image(sha, app_uid)` | look for the tag of that pair, build it when missing with `--build-arg APP_UID`, and refuse an image whose label disagrees |
| `health()` | expects the uid from desired state, read from `/proc/<State.Pid>/status` (the container's main process, present with no strategy running). An unreadable uid **fails**: `capacity_problems` swallows `OSError` and `ValueError`, and this assertion must not. The group check below runs only when the expected uid is not 1000 |
| `write_live()` | records the full tag and `app_uid`, so `make oa-restart` restarts what the pipeline deployed |
| `make oa-verify-image` (called by `oa-restart`) | also checks the running container's uid against `live.json`'s `app_uid`, so a hand restart is checked too, not only pipeline deploys |

**The switch is then one commit:** `"app_uid": 1501` in `production.json`, merged with the pipeline held, then
unheld in a market-closed window. The pipeline builds the exact pinned commit (`ensure_image` builds that sha or
refuses), restarts, installs the release, runs health and writes `live.json`. No `make openalgo` by hand, which
would pull the fork checkout to main's tip, an unsettled commit.

**Rollback is one commit too:** `"app_uid": 1000`. The pipeline builds or finds the plain tag, restarts, and health
expects 1000 and skips the group check. See "Rollback" for what that leaves on disk.

## Keeping the state group-writable

When the uid is not 1000, every file the container creates must be group-readable and group-writable, and every
directory group-writable. A file that is not breaks three things at once: the host's backups, the host tools that read
it, and a rollback image. umask alone does not get there:
- `tempfile.mkstemp` creates 0600, and `mkdtemp` and `TemporaryDirectory` 0700, whatever the umask;
- SQLite creates a new database 0644 & ~umask (`SQLITE_DEFAULT_FILE_PERMISSIONS`; checked with Debian trixie's
  SQLite 3.46.1, the library the image's Python links), and its `-wal` and `-shm` copy that mode;
- `docker exec` processes get runc's default umask, 022;
- some code sets 0755, 0600 or 0700 explicitly.

### When the state is shared

The fork decides it at run time, not at build time: **the state is shared when the process does not own it.**
`utils/state_modes.py` compares `os.getuid()` with the owner of `/app/db` once, and exports the modes every writer
uses: `PRIVATE_FILE_MODE` (0600, or 0660 when shared), `PRIVATE_DIR_MODE` (0700, or 0770), `FILE_MODE` (0644 or
0664), and `share(path)`, which applies the right mode to a file or directory just created. `start.sh` makes the same
comparison with `stat -c %u db`. An upstream install, where the process owns its state, gets today's modes unchanged.
No environment variable is needed, and no Dockerfile branching (an `ENV` cannot branch on a build argument).

### The fork's writers

| Writer | Today | When shared |
| --- | --- | --- |
| `start.sh:183-185` | `chmod -R 755 db log strategies`, `chmod 700 keys` | `umask 002`; `chmod -R g+rwX` on what it owns (it owns nothing that existed before); `keys/` 770 |
| `python_strategy.py:904` (start) and `:2390` (upload) | `os.chmod(file, 0o755)`, clearing group write on every uploaded strategy | adds the execute bits (`mode | 0o111`), never clears the group's |
| `python_strategy.py:941` (log directory, every start) | `os.chmod(dir, 0o755)` | adds the owner's bits (`mode | 0o700`) |
| `utils/oauth_keys.py` | `keys/` re-tightened to 0700, key files to 0600, compared with `st_mode & 0o777` | `PRIVATE_DIR_MODE` (0770) and `PRIVATE_FILE_MODE`; no setgid, so the comparison matches and it stops re-chmodding on every call |
| `services/agent/chatgpt_oauth.py` (`_harden`) | `db/chatgpt_oauth` 0700, its auth file 0600 | the same two constants |
| `services/openscript_store.py:181`, `blueprints/openscript_runner.py:395`, `blueprints/mcp_http.py:641` | `mkstemp` then rename: 0600 | `share()` before the rename |
| `services/research_service.py:254` | `TemporaryDirectory(dir=strategies/research)`: 0700, and left behind by a crash | `share()` on the directory |
| **New SQLite databases**, every engine (`database/engine_factory.create_db_engine`, the one place all SQLite engines are made) | 0644 | a `connect` event that applies `FILE_MODE` to the database file when this process created it, so `-wal` and `-shm` follow |
| DuckDB (`historify.duckdb`) | 664 under umask 002 | unchanged |
| Runs' per-source state, `strategies/state/<id>/` | the process umask | inherits `umask 002` from `start.sh` |
| The /system permissions page | expects 600/700/644/755, and its fix button resets to them | expects the shared modes when shared, so it neither flags every path nor undoes them |

### tmp/ is container-private

The Dockerfile sets `TMPDIR=/app/tmp` (and `NUMBA_CACHE_DIR`, `LLVMLITE_TMPDIR`, `MPLCONFIGDIR` under it), and
`/app/tmp` is bind-mounted from `/srv/data/openalgo/tmp`. Every default `tempfile` call in the container writes there:
the `wars` WhatsApp library's `wars_session_*.db` and its `-wal`/`-shm` (0600; 60 such files today, one live),
`whatsapp_bot_service.py:208` and `:576` (`wars_dump_*`, `openalgo_wa_pair_*`, 0600), pip during deploys and
`TemporaryDirectory` in research (0700; 14 `pip-*` directories today). These are scratch: each is created under a
fresh name by the process that uses it.

So `tmp/` is declared container-private, and excluded from:
- the health group check;
- `docker-run.sh`'s permissions step;
- both backup walks (homelab's `stage()` skips `tmp` today only after `rglob` has walked into it).

Moving `TMPDIR` off the bind mount was considered and not taken: it is upstream's setting, and pip's builds and the
numba cache want the disk the state lives on, not the container's overlay.

### algo-trading's writers

| Writer | Change |
| --- | --- |
| Every `docker exec` into OpenAlgo (`tools/deploy_release.py` pip, `docker-run.sh migrate`) | wrapped as `sh -c 'umask 002; exec ...'`, through one helper |
| `tools/setup_machine.sh:113` (`docker exec ... cp ... && chmod 755` under `set -euo pipefail`) | **the only fatal chmod**: on a 1000-owned adapter the container's chmod fails and kills the script. It installs the adapter from the host instead, as its owner: `install -m 0775 <adapter> "$STATE/strategies/scripts/"`. No `docker exec` |
| `algo_trading/paper/paper_audit.py:403` (`save_panel`) | `os.chmod(tmp, 0o664)` after `mkstemp`. Every panel on disk today is already `-rw-------` |
| `integrations/openalgo/docker-run.sh` | its first-install branch writes `.env` as 660. Before every start, **`make oa-share-state`** (below) on the host user's own files, `tmp/` pruned, and a failure fails the start |
| `tools/openalgo_env_keys.py`, `make-env.sh` | write `.env` as 660, never 600 |
| `deploy_queue.backup` and the homelab backup | walk with `os.walk(onerror=...)` that **raises**, `tmp/` pruned, so an unreadable directory fails the backup instead of vanishing from it (today both use `Path.rglob`, which silently skips one; checked on the host's Python 3.13). `backup()` copies each source database's mode onto its staged copy, which `sqlite3.connect` creates 0644 |

MCP servers started from `~/.claude.json` (`docker exec -i openalgo python /app/mcp/mcpserver.py`) are outside the
helper. They write nothing to the state; if one ever did, the group check would show it.

### One target opens the state, used everywhere

`make oa-share-state` runs, as the host user, on the files that user owns, with `tmp/` pruned:
`find "$STATE" -path "$STATE/tmp" -prune -o -user "$(id -u)" -type d -exec chmod g+rwx {} + -o -user "$(id -u)" -type f
-exec chmod g+rw {} +`, then `.env` 660 and `keys/` 770 with 660 files. `-user` is repeated on both branches on
purpose: `-o` binds looser than the implied `-a`, so a single `-user` before two branches would leave the file branch
unguarded, and it would chmod 1501's files and fail. Run in a scratch tree, the command leaves `tmp/` untouched and
the group check below then prints nothing; the other-owner case needs a second uid and is part of the planted check. The paths come from `docker inspect openalgo
--format '{{json .Mounts}}'`, not assumed (if `ALGO_PAPER_DATA` were ever set, `paper_data` would live elsewhere).
It is idempotent and cheap, and it is what:
- `docker-run.sh` runs before every start, failing the start if it fails;
- the migration runs once by hand (step 4);
- a restored node gets on its first start. A restore extracts as uid 1000, which is this design's ownership, but not
  with its modes: the archive holds databases at 0644, the tar's `./` entry carries the stage directory's 0700, and
  non-root GNU tar applies the runner's umask 022 (checked with a scratch tar). The host user owns all of it, so the
  first `docker-run.sh` start repairs it.

### A standing check, not a one-off

Health, on every pipeline deploy, and `oa-verify-image`, on every hand restart, assert:
1. **The container's uid is the desired one,** from desired state; unreadable fails.
2. **When that uid is not 1000, nothing in the state is closed to the group:** `find "$STATE" -path "$STATE/tmp" -prune
   -o \( -type f ! -perm -g=rw -print \) -o \( -type d ! -perm -g=rwx -print \)` prints nothing. A writer that
   regresses is caught at the next deploy, not at the next rollback or backup.

## What reads and writes the state

| Who | What it does | After the change |
| --- | --- | --- |
| Strategies, gunicorn, the websocket proxy, `docker exec` (MCP servers, pip, migrate) | write db, log, paper_data, strategies; scratch in tmp | uid 1501, through the group on existing files and as owner of new ones, with the shared modes. These are the container's own threads, which the limit should count |
| `make deploy` / `tools/deploy_release.py` | `docker exec ... pip install` into `/app/.venv` | runs as 1501 under `umask 002`; `/app/.venv` is `COPY --chown=appuser`, so owned by 1501 |
| `paper_watchdog` (cron, uid 1000) | reads `paper_audit/`; reads `settings.analyze_mode` from `db/openalgo.db` | group read. SQLite fchmods `-wal`/`-shm` to the database file's mode, so a group-writable database file is enough |
| `refresh_results_calendar` (cron, uid 1000) | writes `paper_data/results_calendar.csv` | owner of its file; the container only reads it |
| market-recorder's TBT start (`tick_recorder.tbt_decisions_dir`, uid 1000) | reads `paper_audit/*.jsonl` | group read. A `PermissionError` there falls into `start_tbt`'s broad except and records the day without the 50-level names, so the group check guards it |
| `make gate`, `make reconstruct`, `make report` (uid 1000) | read the audit files and `paper_panels/*.json` | group read, once `save_panel` writes 664 |
| `tools/whatsapp_group_link.py` (`make alerts-here`, `make alerts-reset`) | **writes** `db/openalgo.db` from the host | owner of the existing file |
| `deploy/deploy_window.py` | reads `db/openalgo.db` read-only | owner |
| `algo_trading/paper/sandbox_mode.py`, `integrations/openalgo/login-check.sh`, `tools/status.sh`, `tools/doctor.py` | read the state | owner, or group read |
| `tools/openalgo_env_keys.py`, `make-env.sh` | write `.env` | 660 (above) |
| `tools/setup_machine.sh` | installs the strategy adapter | from the host, as owner, 0775 (above) |
| `deploy_queue.backup` (runner, uid 1000) | stages db, strategies and `.env`, then `tar -c` | group read; fails loudly on anything unreadable; `tmp/` pruned |
| homelab backup (user timer, uid 1000) | SQLite's online backup, then restic | group read, group write for `-shm`; fails loudly on anything unreadable; `tmp/` pruned |
| `deploy/setup_node.sh --restore` | `tar -x` as uid 1000, then `rsync` for `keys/` and `paper_data/` | owner 1000; modes repaired by `oa-share-state` on the first start |
| `make oa-move-state` | renames entries within one filesystem | group write on the directories |
| Research scripts | open `historify.duckdb` read-only | group read |
| The Actions runner | `docker` group, `live.json`, oa-* targets | `live.json` carries the full tag and `app_uid` |

## Migration

In a market-closed window. Every guard is checked, not assumed.
1. **Merge the fork change.** With the process owning its state it behaves exactly as today, so it deploys like any
   other change, as a plain `openalgo-fork:<sha>` tag.
2. **Merge the algo-trading change** with `app_uid` still absent (1000): `live()`, `decide()`, the images, health,
   `write_live`, `oa-verify-image`, `oa-share-state`, the writers and the backups. It deploys like any other change and
   switches nothing.
3. **Hold the pipeline, and confirm it:** `make queue-hold REASON="dedicated uid"`, then `make deploy-status` must show
   the hold. `queue-hold` only dispatches a workflow, so it can return before the hold lands.
4. **Open the existing state to the group:** `make oa-share-state`, as the host user, with the container still running
   (changing modes does not disturb it). Then the group check must print nothing.
5. **Reserve the uid** (the operator's `sudo` step): `sudo useradd --system --uid 1501 --gid 1000 --no-create-home
   --shell /usr/sbin/nologin openalgo`.
6. **Stop the collector timers:** `systemctl --user stop 'collect-*.timer'`, and check that no `collect@*` unit is
   active. Once OpenAlgo is down, `oa-restart`'s collector check reports idle without looking, and
   `collect-retry.timer` fires in the evening (18:40 and 19:00 observed).
7. **The switch:** merge `"app_uid": 1501` into `production.json`, then `make queue-unhold`. The pipeline converges at
   its next slot: it builds `openalgo-fork:<pinned sha>-u1501`, restarts, installs the release, runs health (uid 1501,
   group check) and writes `live.json`.
8. **The planted check** below.
9. **Resume:** `systemctl --user start 'collect-*.timer'`.
10. **Record the change:** CLAUDE.local.md and the fork-setup notes say the container runs as `app_uid`, with state
    owned by 1000 and shared through group 1000.

## Rollback

**One commit:** `"app_uid": 1000` in `production.json` (or remove the key). The pipeline finds or builds the plain tag
of the pinned commit, restarts, and health expects 1000 and skips the group check. On disk:
- **Files that existed before the switch** are owned by 1000, the image's own uid. Nothing to do.
- **Files uid 1501 created** are group-writable, because every writer above shares them and the standing check has
  refused any that were not. The uid-1000 image reaches them through the group. Its `start.sh` runs `chmod -R 755 db
  log strategies`, which succeeds on the 1000-owned files and clears their group write. That is harmless while the
  uid is 1000, and the next switch forward repairs it: `docker-run.sh` runs `oa-share-state` before the start.
- **Leftovers in `tmp/`** owned by 1501 stay where they are. Nothing reads another process's scratch, and they are
  outside every check.
- **If the group check had been failing,** some 1501-owned files are closed to uid 1000, and only root can open them:
  `sudo chown -R 1000 /srv/data/openalgo`, then `make oa-share-state`. This is the one case that needs a hand, and the
  standing check exists so that it never arises.

## The planted check

It proves the limit counts only the container's threads. It runs once after step 7; the standing guards are the
health and `oa-verify-image` assertions.
1. **The uids:**
   - `docker exec openalgo id -u` prints 1501, and `live.json` names the `-u1501` tag and `app_uid` 1501;
   - a strategy started by the real launcher (its Start button) shows `Uid: 1501 1501 1501 1501` in
     `/proc/<pid>/status`, and `Max processes 4096 4096` in `/proc/<pid>/limits`;
   - `stat -c %u /srv/data/openalgo/db/openalgo.db` still prints 1000;
   - with 1501-owned files now present (logs written since the switch), `make oa-share-state` exits 0: it touched
     only the host user's files.
2. **The control, measured, not assumed:** `ps -L -U 1000 --no-headers | wc -l` (real uid, which is what
   `RLIMIT_NPROC` counts; `-u` would select the effective uid) is recorded and must exceed 200. Under the old uid that
   whole count sat inside every strategy's limit.
3. **Host threads do not count:**
   - `C=$(ps -L -U 1501 --no-headers | wc -l)` and `N=$((C + 20))`.
   - As the host user, hold 300 extra threads: `python3 -c "import threading,time; [threading.Thread(target=time.sleep,
     args=(120,), daemon=True).start() for _ in range(300)]; time.sleep(120)" &`.
   - `docker exec openalgo /app/.venv/bin/python -c "import resource,threading;
     resource.setrlimit(resource.RLIMIT_NPROC, ($N,$N)); threading.Thread(target=lambda: None).start();
     print('started')"` prints `started`.
4. **Container threads do count:**
   - Hold 50 threads inside the container: `docker exec -d openalgo /app/.venv/bin/python -c "import threading,time;
     [threading.Thread(target=time.sleep, args=(120,), daemon=True).start() for _ in range(50)]; time.sleep(120)"`.
   - Run check 3's last command again with the same `N`: it now fails with `can't start new thread`.
5. **The same command as uid 1000 fails,** which is what makes 3 meaningful. With the host's 300 threads still held,
   run check 3's last command as `docker exec --user 1000 openalgo ...` with `N=$(( $(ps -L -U 1000 --no-headers |
   wc -l) + 20 ))` measured just before the 300 start: it fails with `can't start new thread`, because the host's
   threads now count. That is the 2026-10-28 failure, reproduced on purpose and without a uid-1000 image. It is the
   only deliberate `docker exec --user` in this design.

## Considered and not taken

- **Chowning the state to 1501** (the first draft). It needs `sudo` to roll back, because a uid-1501 owner can close
  files to the group and only root can reopen them.
- **The uid only in the image tag** (the first revision). `live()` cannot parse it, and nothing records it for a hand
  restart, so the pipeline would restart every half hour and hand restarts would revert.
- **Moving `TMPDIR` off the bind mount.** Upstream's setting, and pip and the numba cache want the state's disk.
- **Default ACLs** (`setfacl -d -m g::rwX`). They make every writer's umask irrelevant, but need the `acl` package on
  the host, and do not help a file created with an explicit mode or a new SQLite database. The writer fixes are needed
  either way.
- **Docker user-namespace remapping** (`userns-remap`). It is daemon-wide, it remaps every container and bind mount,
  and it changes every file's numeric owner. This proposal does the same job for one container with one build
  argument.

## Not covered

- **The 4096 limit:** `STRATEGY_NPROC_LIMIT` stays. With a dedicated uid it bounds OpenAlgo's own threads, which is
  what it was meant to do.
