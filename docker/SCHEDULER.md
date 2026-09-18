# Dedicated recurring-job scheduler

## Current rollout status

The replacement web service is running and healthy. The dedicated scheduler
has **not been activated**, in accordance with the owner's instruction not to
change existing company data. Recurring HR automation is therefore paused.
Interactive web actions remain available and may still change records when
users perform them; this is not a database-wide read-only mode.

The scheduler has an explicit `scheduler` Compose profile. Ordinary
`docker compose up` does not start it. Naming the service explicitly (as in the
activation instructions below), or enabling `--profile scheduler`, does start
it and permits its HR jobs to modify records. Do not activate it without approval.

Verification completed before the data-change restriction:

- Django system checks: passed.
- Scheduler/report regression suite: **59 tests passed**, including in the
  rebuilt image. HR repeat-effect tests use mocks, not production records.
- Registration-only dry-run: 25 core schedules, no HR functions executed.
- Paused dedicated-process probe and PostgreSQL leadership contention: passed.
- Execution-lock contention and temporary-table rollback probe: passed;
  no business rows were used.
- Isolated two-worker Gunicorn: HTTP 200 with APScheduler startup forbidden.
- Migration plan and subsequent web startup: no migrations to apply.
- Production Compose structure: validated using dummy settings only; production
  deployment and all real business-job executions remain unverified.

After the restriction, checks were limited to source/configuration inspection
and container status. The attempted old-image rollback tag could not be created
because Docker could not resolve the previous image ID; do not assume that tag
exists. Use a separately retained release artifact for image rollback.

## Root cause and architecture

Previously, package imports, `AppConfig.ready()`, migration-package imports and
biometric web handlers constructed `BackgroundScheduler` instances. Gunicorn's
`preload=False` means each worker imports the application separately. Its worker
recycling and development reloads also recreated schedulers. Most legacy argv
guards excluded migrations but not tests or collectstatic; PMS was unconditional.
APScheduler's per-process job IDs and `max_instances` cannot coordinate workers.

Before: each web worker → multiple independent in-memory schedulers → shared DB.

After:

```text
Browser → Nginx (production profile) → Gunicorn web workers → PostgreSQL / Redis
                                     no recurring schedulers
Dedicated run_scheduler process → PostgreSQL leadership + execution locks
                                → existing HR job functions
                                → shared Redis readiness heartbeat
```

Entry points: `base/management/commands/run_scheduler.py` and
`base/management/commands/scheduler_health.py`. The project package `horilla`
is not an installed Django application, so commands belong under `base`.
Registration lives in `horilla/scheduling.py`; locks/transactions live in
`horilla/scheduler_runtime.py`. No business model migration was added.

## Verified job inventory

All legacy startup locations below could multiply jobs across web workers.
Device POST handlers could additionally create multiple schedulers in one worker.
The current registry preserves the actual former schedules, including the
four-hour roster cadence (its old docstring incorrectly described 00:05).

| Module/functions | Schedule | Former startup | Duplicate effects / existing guards |
| --- | --- | --- | --- |
| `base/scheduler.py`: `rotate_shift`, `rotate_work_type` | 4 hours each | `base/__init__.py` | Shift/work-type changes and notifications; next-change markers, formerly unprotected across processes |
| same: `undo_shift`, `switch_shift`, `undo_work_type`, `switch_work_type` | 4 hours each | same | Updates/notifications; changed/active flags |
| same: `recurring_holiday`, `sync_roster_shifts` | 4 hours each | same | Holiday dates and employee shifts; state/date checks |
| `employee/scheduler.py`: `update_experience` | 4 hours | `employee/__init__.py` | Repeated derived-field writes |
| same: `block_unblock_disciplinary` | 60 seconds | same | Repeated account status changes |
| `leave/scheduler.py`: `leave_reset` | 4 hours | `leave/__init__.py` | Balances/carryforward; reset/expiry dates advance, previously separate commits |
| `attendance/scheduler.py`: `create_work_record` | 30 minutes **and** 00:30 | `attendance/apps.py` | Unique employee/date records and conflict-ignoring bulk insert; both schedules retained |
| same: `auto_punch_out` | 5 minutes | same | Clock-out writes; filters open attendance/activity records |
| same: `send_check_in_reminders`, `send_missing_checkout_reminders` | 08:30, 18:00 | same | Notifications; recipient/verb/date existence check |
| `payroll/scheduler.py`: `expire_contract` | 4 hours | `payroll/apps.py`, migration package | Repeated state updates; active-contract filter |
| same: `auto_payslip_generate` | 3 hours | same | Payroll recalculation/writes; period lookup and save helper; corrected mid-contract lookup |
| `recruitment/scheduler.py`: `candidate_convert`, `recruitment_close` | 5 minutes, 1 hour | migration package | Converted/closed state filters; fixed frozen import-time date |
| `pms/scheduler.py`: `cyclic_feedback_creation` | 08:00 | `pms/__init__.py`, unconditional | Feedback creation; disables source flag after creating copy |
| `asset/scheduler.py`: `notify_expiring_assets`, `notify_expiring_documents`, `mark_expired_assets` | 1 day, 4 hours, 1 day | `asset/__init__.py` | Notifications previously had no repeat guard; expiry updates filter status |
| `report/scheduler.py`: `run_report_subscriptions` | 1 hour | `report/apps.py` | Email delivery; subscription last-run interval checks |
| `outlook_auth/scheduler.py`: `refresh_outlook_auth_token` | 50 minutes | package import | External token refresh; optional app, not installed in current configuration |
| `pg_backup/scheduler.py`: `backup_postgres` | `BACKUP_CRON_TIMES`, default 02:00/10:00/18:00 | `pg_backup/apps.py` | External backup files; optional app, not installed currently |
| `biometric/views.py`: ZK, Anviz, Dahua, COSEC, eTimeOffice poll functions | Per-device duration | URL import plus FBV/CBV scheduling requests | Device reads/attendance writes; flags/cursors vary by driver |
| `horilla_backup/scheduler.py`: `google_drive_backup` | Active DB configuration: interval or cron | Start/stop web actions | External uploads/files; no end-to-end delivery idempotency |

Device and Drive configuration is reconciled every 15 seconds. Unchanged timers
are not reset. Disabled schedules are removed. Device IDs are bound explicitly,
avoiding the old import-loop lambda closure problem. The import-time bulk reset
of device `is_live` flags was removed. Web actions now save configuration only.

Request-triggered email/export/automation threads and explicitly activated live
biometric capture threads are not recurring APScheduler jobs; they were not
removed. This change does not make those separate thread-based workflows durable.

## Guarantees and limits

- PostgreSQL session advisory lock `731924118` admits one scheduler per database.
  Use a **direct connection or session pooling**, not PgBouncer transaction pooling.
- Leadership uses a dedicated connection; ORM connection cleanup cannot release
  it. Loss of that connection exits the scheduler; Compose restarts it.
- A single executor serializes jobs. A transaction-scoped advisory lock
  `731924119` also prevents overlap with a job still running during failover.
  Failed jobs roll back their synchronous default-database writes.
- Every registration has a stable ID, replacement, coalescing, one maximum
  instance and finite misfire grace. Different work-record triggers cannot overlap.
- Shutdown stops scheduling and waits for in-flight work before releasing
  leadership. Compose allows five minutes; a forced kill can still interrupt work.
- Expiry notifications have per-object/per-expiry keys stored in notification
  JSON. This assumes the project's `USE_JSONFIELD=True`; deleting notification
  history also deletes this deduplication evidence.
- Payroll now looks up the effective, contract-adjusted date range before
  recalculating an existing payslip. Leave and PMS changes run transactionally
  when invoked by the scheduler. This is not a redesign of manual web actions.
- **This is not exactly-once external delivery.** An email/upload/device action
  can succeed before a crash rolls back its DB marker. An outbox/provider
  idempotency design is still required for that guarantee. Existing jobs that
  swallow exceptions or spawn threads also limit transaction guarantees.
- In-memory interval timers restart on scheduler restart. There is no durable
  replay of schedules missed while the process was down. Long jobs can delay
  others; monitor missed/max-instance events and job runtimes.
- Readiness proves a live leader/reconciler, not successful business outcomes.
  Alert separately on job failures and missed schedules.

## Development and production

For local non-Docker use, configure PostgreSQL and `REDIS_URL`, migrate once,
then run `python manage.py runserver` and, in a separate terminal,
`python manage.py run_scheduler`. Web-only development is supported; no jobs run
until the second command is started. SQLite is deliberately rejected for live
scheduling because it does not provide the distributed lock used here.

Safe registration check:

```sh
docker compose exec -T web python manage.py run_scheduler --dry-run
```

Safe leadership/startup probe (all jobs paused, no HR functions executed):

```sh
docker compose exec -T web python manage.py run_scheduler --probe 10
```

A paused probe intentionally fails readiness. It must not be run alongside a
real scheduler unless testing rejection of a competing process.

Deployment order is important: old web workers do not honor the new lock.
**Do not start the new scheduler while any old workers are still alive.**

1. Build both images: `docker compose build web scheduler`.
2. Stop any external/old scheduler processes, if present.
3. Recreate web: `docker compose up -d --no-deps --force-recreate web`.
4. Wait for web health and confirm its logs contain no scheduler starts.
5. Start the scheduler: `docker compose up -d --no-deps scheduler`.
6. Check `docker compose exec -T scheduler python manage.py scheduler_health`
   and `docker compose logs --tail 100 scheduler`.

Step 5 is an explicit activation of data-changing automation, not a read-only
verification step. It is currently deferred at the owner's request.

For production, use the same commands with
`-f docker-compose.yml -f docker-compose.prod.yml`. Supply all required production
secrets from `.env`. The overlay removes scheduler source mounts, shares DB/Redis
configuration with web, and enables Nginx. TLS termination must be configured
for your deployment. The existing direct web port remains exposed as before;
restrict it at the firewall or change the binding when deploying behind Nginx.

The scheduler entrypoint skips migrations and collectstatic; Compose waits for
healthy web first. Both services share the image build and media volume. Merely
editing source does not replace `/entrypoint.sh` in an already-built image;
rebuild before using the dedicated service.

## Verification and known operational findings

Regression suite:

```sh
docker compose exec -T web python manage.py check
docker compose exec -T web python manage.py test base.tests.test_scheduler report.tests.test_standard_reports --noinput
```

The scheduler suite checks all 25 core registrations, duplicate registration
before/after start, dynamic device/backup changes, import safety for WSGI,
runserver/migrate/test/collectstatic/shell, leadership rejection, execution-lock
skips, transaction error propagation, and mocked payroll/leave/PMS/notification
repeat behavior. These focused tests are not a full HR integration suite.

Independent PostgreSQL probes verify competing-process rejection, real rollback
using a temporary table (no business rows), and exclusion by a separately held
execution lock. A separate two-worker Gunicorn probe forbids APScheduler startup
and checks `/health/` on an unexposed loopback port.

Existing issues outside the duplication fix:

- Django warns about database access during application initialization; other
  startup code still performs queries. That warning is not a scheduler start.
- Google Drive's existing job references `service_account_file`, while the
  current model exposes OAuth fields. Its backup implementation needs a separate
  repair before enabling it. No active Drive/device schedules were registered in
  the observed dry-run. The runtime image also lacks `pg_dump`; optional PostgreSQL
  backups need a compatible client installed and persistent backup storage.
- Current local `.env` lacks required production secrets; production startup
  cannot be verified against real production settings here.
- Three pre-existing deleted AI-assistant files in Git were left untouched.

## Rollback

Stop the dedicated scheduler first. To pause automation safely, leave the new
web code deployed and keep that service stopped; interactive HR remains usable.
No schema rollback is needed. Restore the previous release/image only using
your normal release process, preserving unrelated workspace changes. Restoring
legacy web code reintroduces import-time schedulers: do **not** run it alongside
the dedicated service. A single legacy worker reduces worker duplication but
does not fix repeated biometric scheduling requests or crash delivery semantics.
