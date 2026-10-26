# Testing upgrades

OpenNVR is in production with clients. Clients never reinstall: they pull a
new image onto a stack that already holds months of cameras, users,
recordings, app config and an Alembic history. Since 2026-10-26, **every change
is tested as an upgrade of an existing install, not as a fresh install.**

A clean install passing proves very little. The failures that reach clients
only show up on an upgrade:

- a migration that is out of order or collides with another branch's revision
  (929ec8e0 had to re-id one for exactly this reason)
- a schema that `create_all` builds fine but Alembic never reaches
- files, volumes or permissions left behind by an older version
- settings or rows the new code reads differently from how the old code wrote
  them

## The rule

1. Test on the **shared test setup** that already has data in it. Don't
   `down -v`, don't delete volumes, and don't build a throwaway stack for the
   main check.
2. Get it to the **current production version** first, then upgrade it to your
   branch. You're testing the jump a client will make.
3. **Back up before every upgrade**, so the next person can start from the same
   state.
4. Check that **the old data still works**, not just your feature.
5. If a test really needs a fresh install, agree it with the team first, and
   still do the upgrade test as well.

The isolated E2E suite ([TESTING.md](TESTING.md)) and unit tests still run as
usual. They add to the upgrade test. They don't replace it.

## Procedure

Commands assume the default container names from `docker-compose.yml` and that
`POSTGRES_USER` / `POSTGRES_DB` come from your `.env`.

### 1. Record where you're starting from

```bash
docker exec opennvr_db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "SELECT version_num FROM alembic_version;"
docker image inspect ghcr.io/open-nvr/core:main --format '{{.Id}} {{.Created}}'
```

Write both down in the PR. If the stack isn't on the current production
version, upgrade it to that first and check that step works too.

### 2. Back up the database

Dump **inside** the container, then copy the file out:

```bash
docker exec opennvr_db pg_dump -U "$POSTGRES_USER" -Fc -d "$POSTGRES_DB" -f /tmp/pre-upgrade.dump
docker cp opennvr_db:/tmp/pre-upgrade.dump ./backups/pre-upgrade-$(date +%F).dump
```

> **Windows:** don't run `pg_dump ... > file` in PowerShell 5.1. It re-encodes
> the binary archive as UTF-16 and silently corrupts it. A good dump starts
> with the bytes `PGDMP`.

### 3. Upgrade in place

Build your branch's core image and switch only core to it:

```bash
docker build -t ghcr.io/open-nvr/core:<branch> .
CORE_TAG=<branch> docker compose -f docker-compose.yml up -d --no-deps opennvr-core
```

(PowerShell: `$env:CORE_TAG='<branch>'; docker compose ...`)

- Use `--no-deps`. Don't use `--remove-orphans`, because it deletes containers
  from other compose files or profiles (fake cameras, apps).
- `start.ps1 up` / `start.sh up` **never pull**, so a stale image is reused
  without any warning. Pull or build explicitly.
- If your change touches `sdk/opennvr-app-sdk`, rebuild the app images too. The
  SDK is baked into each one.

Core runs its migrations by itself on startup.

### 4. Check the migration

Core's logs go to files inside the container, not to `docker logs`:

```bash
docker exec opennvr_core sh -c "grep -i -E 'alembic|migrat' /app/logs/server.log | tail -20"
docker exec opennvr_db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "SELECT version_num FROM alembic_version;"
```

The revision must now be your branch's head, with no errors in the log. Run
the same check a second time after restarting core. A migration must be safe
to run against a database that's already at head.

### 5. Check the old data still works

Go through this list on the upgraded stack:

- [ ] Existing admin and normal users can log in (and MFA still works for those
      who enrolled)
- [ ] Every camera that was online before is still online and streams live
- [ ] Recordings made **before** the upgrade still play back, and new ones are
      being written
- [ ] Existing alerts, events and visits still open, evidence images included
- [ ] Installed apps and adapters are still registered and keep their camera
      assignments and settings
- [ ] Settings changed before the upgrade (retention, recording, notifications)
      kept their values
- [ ] Your new feature works on this old data, not only on data created after
      the upgrade

### 6. Write it in the PR

Add a short **Upgrade tested** section to the PR:

```
Upgrade tested: main @ <image id / date>, alembic <old rev>  ->  <branch>, alembic <new rev>
Data on stack: <n> cameras, <n> users, recordings since <date>, apps: <list>
Checklist: all passed / <what failed>
```

A PR that changes the schema, config files, volumes, or the format of stored
data isn't ready for review without this section.

## If the upgrade breaks

Restore the backup, then go back to the previous image:

```bash
docker compose -f docker-compose.yml stop opennvr-core
docker cp ./backups/pre-upgrade-<date>.dump opennvr_db:/tmp/restore.dump
docker exec opennvr_db pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists /tmp/restore.dump
docker compose -f docker-compose.yml up -d --no-deps opennvr-core
```

Then fix the migration so it works on the old database. Don't fix it by
resetting the test stack. A client can't reset theirs.
