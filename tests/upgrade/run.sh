#!/usr/bin/env bash
# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# The upgrade test (docs/design/auto-update-implementation-plan.md, UPD-207):
# real updates, through ./start.sh update, on a real stack, with data.
#
# From this checkout it makes three releases, each a commit, a core image
# (built from `git archive`, tracked files only) and a signed offline bundle:
#   99.0.0  this checkout
#   99.0.1  + a migration (table upd_test_b)
#   99.0.2  + another migration (table upd_test_c), and it reports the wrong
#           version, so it fails verification after the database migrated
# then installs 99.0.0, puts a marker row in its database, and checks:
#   1. 99.0.0 -> 99.0.1 updates, migrates and keeps the data;
#   2. 99.0.1 -> 99.0.2 is caught and rolled back: 99.0.1 again, the
#      database restored (no upd_test_c), the data kept, the checkout clean;
#   3. a bundle changed by one byte is refused before anything changes;
#   4. 99.0.3 (good again) is found on a local mirror (UPDATE_SOURCE_URL,
#      a plain HTTP folder) and installed from the bundle it serves.
#
# Linux, Docker with compose, git, python3 with `cryptography`. Uses the
# host's ports 18080/18443 and the opennvr_* container names: never run it next
# to a stack you care about. Run from anywhere:
#     tests/upgrade/run.sh            (UPGRADE_TEST_KEEP=1 to leave it all up)

set -euo pipefail

REPO=$(cd "$(dirname "$0")/../.." && pwd)
WORK=${UPGRADE_TEST_DIR:-$(mktemp -d /tmp/opennvr-upgrade.XXXXXX)}
PY=${PYTHON:-python3}
V0=99.0.0 V1=99.0.1 V2=99.0.2 V3=99.0.3

step() { echo; echo "===== $*"; }
fail() { echo "UPGRADE TEST FAILED: $*" >&2; exit 1; }
cleanup() {
    [ "${UPGRADE_TEST_KEEP:-}" = 1 ] && { echo "kept: $WORK"; return; }
    [ -d "$WORK/site" ] && (cd "$WORK/site" && docker compose -f docker-compose.yml down -v --remove-orphans >/dev/null 2>&1 || true)
    [ -n "${MIRROR_PID:-}" ] && kill "$MIRROR_PID" 2>/dev/null
    # The stack's containers left root-owned files (certificates, data):
    # remove them as root, with a test image that is still here.
    [ -d "$WORK/site" ] && docker run --rm --user 0 --entrypoint rm -v "$WORK:/w" \
        "ghcr.io/open-nvr/core:$V0" -rf /w/site >/dev/null 2>&1
    docker rmi "ghcr.io/open-nvr/core:$V0" "ghcr.io/open-nvr/core:$V1" "ghcr.io/open-nvr/core:$V2" \
        "ghcr.io/open-nvr/core:$V3" >/dev/null 2>&1 || true
    git -C "$WORK/rel" worktree prune 2>/dev/null || true
    rm -rf "$WORK"
}
trap cleanup EXIT

sql() { docker exec opennvr_db sh -c "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -Atc \"$1\""; }
version() { docker exec opennvr_core curl -s localhost:8000/health | sed -n 's/.*"version":"\([^"]*\)".*/\1/p'; }
tables() { sql "select string_agg(tablename, ',' order by tablename) from pg_tables where tablename like 'upd_test%'"; }
wait_healthy() {
    for _ in $(seq 1 90); do
        [ "$(docker inspect opennvr_core --format '{{.State.Health.Status}}' 2>/dev/null)" = healthy ] && return 0
        sleep 5
    done
    return 1
}
expect() { # what, got, wanted
    if [ "$2" = "$3" ]; then echo "  ok   $1: $2"; else fail "$1 is '$2', expected '$3'"; fi
}

step "three releases from $(git -C "$REPO" rev-parse --short HEAD)"
git clone -q "$REPO" "$WORK/rel"
cd "$WORK/rel"
git checkout -q -B upgrade-test "$(git -C "$REPO" rev-parse HEAD)"
git config user.email upgrade-test@opennvr.invalid
git config user.name "upgrade test"
base_head=$($PY -c "import sys; sys.path[:0]=['scripts/release','server']; import make_manifest as m; from pathlib import Path; print(m.alembic_graph(Path('server/migrations/versions'))[0])")
migration() { # revision, down revision, table
    $PY - "$@" <<'PY'
import sys
rev, down, table = sys.argv[1:4]
open(f"server/migrations/versions/{rev}_upgrade_check.py", "w", newline="\n").write(f'''"""Upgrade test release migration (tests/upgrade/run.sh)."""
import sqlalchemy as sa
from alembic import op

revision = "{rev}"
down_revision = "{down}"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("{table}", sa.Column("id", sa.Integer, primary_key=True))


def downgrade() -> None:
    op.drop_table("{table}")
''')
PY
}
release() { # version, version the image reports
    sed -i "s/^__version__ = \".*\"/__version__ = \"$2\"/" server/main.py
    sed -i "s/^CORE_TAG=.*/CORE_TAG=$1/" .env.example
    git add -A && git commit -q -m "upgrade test release $1" && git tag -f "v$1" >/dev/null
}
release "$V0" "$V0"
migration 99aa00000001 "$base_head" upd_test_b; release "$V1" "$V1"
migration 99aa00000002 99aa00000001 upd_test_c; release "$V2" "$V2-broken"
release "$V3" "$V3"
for v in $V0 $V1 $V2 $V3; do
    git archive --format=tar "v$v" | docker build -q -t "ghcr.io/open-nvr/core:$v" \
        --build-arg "GIT_COMMIT=$(git rev-parse "v$v^{commit}")" - >/dev/null
    echo "  built core:$v"
done
$PY "$REPO/tests/upgrade/make_bundles.py" "$WORK" $V0 $V1 $V2 $V3

step "install $V0 from its bundle"
git clone -q "$WORK/rel" "$WORK/site"
cd "$WORK/site"
git checkout -q -B opennvr-release "v$V0"
cp .env.example .env
bash scripts/generate-secrets.sh --write >/dev/null 2>&1
sed -i -e "s/^CORE_TAG=.*/CORE_TAG=$V0/" -e 's/^OPENNVR_DEFAULT_APPS=.*/OPENNVR_DEFAULT_APPS=off/' .env
# No detect-pipeline image exists for the test versions.
echo "DETECT_PIPELINE_IMAGE=ghcr.io/open-nvr/detect-pipeline:0.1.8" >> .env
# Nothing here goes through nginx from the host: keep it off 80/443.
printf 'NGINX_HOST_HTTP_PORT=18080\nNGINX_HOST_HTTPS_PORT=18443\n' >> .env
mkdir -p "$WORK/b0" && tar -xf "$WORK/opennvr-$V0-offline.tar" -C "$WORK/b0" images.tar
docker load -q -i "$WORK/b0/images.tar" >/dev/null && rm -rf "$WORK/b0"
./start.sh up > "$WORK/install.log" 2>&1 || { tail -40 "$WORK/install.log"; fail "./start.sh up"; }
wait_healthy || fail "core did not come up healthy on $V0"
sql "create table upd_test_marker(note text); insert into upd_test_marker values ('seeded on $V0')" >/dev/null
marker=$(sql "select note from upd_test_marker")
expect "version" "$(version)" "$V0"
expect "migration" "$(sql 'select version_num from alembic_version')" "$base_head"

export OPENNVR_UPDATE_EXTRA_KEYS="$WORK/keys.json"

step "1. $V0 -> $V1: a good update"
OPENNVR_UPDATE_VERIFY_TIMEOUT=240 ./start.sh update --bundle "$WORK/opennvr-$V1-offline.tar" --yes || fail "the update to $V1"
expect "version" "$(version)" "$V1"
expect "migration" "$(sql 'select version_num from alembic_version')" 99aa00000001
expect "test tables" "$(tables)" "upd_test_b,upd_test_marker"
expect "marker" "$(sql 'select note from upd_test_marker')" "$marker"
expect "pin" "$(grep '^CORE_TAG=' .env)" "CORE_TAG=$V1"
expect "checkout" "$(git rev-parse HEAD)" "$(git -C "$WORK/rel" rev-parse "v$V1^{commit}")"
expect "changed files" "$(git status --porcelain --untracked-files=no | wc -l | tr -d ' ')" 0
expect "history" "$(tail -1 data/updates/history.log | awk '{print $2, $3, $4}')" "$V0 $V1 succeeded"

step "2. $V1 -> $V2: a broken release is rolled back"
if OPENNVR_UPDATE_VERIFY_TIMEOUT=90 ./start.sh update --bundle "$WORK/opennvr-$V2-offline.tar" --yes; then
    fail "the broken release was accepted"
fi
wait_healthy || fail "core is not healthy after the rollback"
expect "version" "$(version)" "$V1"
expect "migration" "$(sql 'select version_num from alembic_version')" 99aa00000001
expect "test tables" "$(tables)" "upd_test_b,upd_test_marker"
expect "marker" "$(sql 'select note from upd_test_marker')" "$marker"
expect "pin" "$(grep '^CORE_TAG=' .env)" "CORE_TAG=$V1"
expect "checkout" "$(git rev-parse HEAD)" "$(git -C "$WORK/rel" rev-parse "v$V1^{commit}")"
expect "changed files" "$(git status --porcelain --untracked-files=no | wc -l | tr -d ' ')" 0
expect "history" "$(tail -1 data/updates/history.log | awk '{print $2, $3, $4}')" "$V1 $V2 rolled_back"

step "3. a bundle changed by one byte is refused"
mkdir -p "$WORK/tamper" && tar -xf "$WORK/opennvr-$V2-offline.tar" -C "$WORK/tamper"
sed -i "s/\"version\": \"$V2\"/\"version\": \"99.9.9\"/" "$WORK/tamper/release-manifest.json"
(cd "$WORK/tamper" && tar -cf "$WORK/tampered.tar" ./*)
before=$(docker inspect opennvr_core --format '{{.Id}}')
if ./start.sh update --bundle "$WORK/tampered.tar" --yes; then fail "the tampered bundle was accepted"; fi
expect "core container" "$(docker inspect opennvr_core --format '{{.Id}}')" "$before"
expect "version" "$(version)" "$V1"

step "4. $V1 -> $V3 from a local mirror"
# The layout scripts/make-offline-bundle.py --mirror-dir writes.
platform=$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}')
mirror="$WORK/mirror"
mkdir -p "$mirror/latest" "$mirror/v$V3"
tar -xf "$WORK/opennvr-$V3-offline.tar" -C "$mirror/latest" release-manifest.json release-manifest.json.sig
cp "$mirror/latest/"release-manifest.json* "$mirror/v$V3/"
cp "$WORK/opennvr-$V3-offline.tar" "$mirror/v$V3/opennvr-${platform//\//-}-offline.tar"
# Served on the stack network's gateway: the host and the updater's
# container both reach it there.
gateway=$(docker network inspect site_opennvr_internal --format '{{(index .IPAM.Config 0).Gateway}}')
(cd "$mirror" && exec $PY -m http.server 8765 --bind 0.0.0.0 >/dev/null 2>&1) &
MIRROR_PID=$!
sleep 2
UPDATE_SOURCE_URL="http://$gateway:8765" OPENNVR_UPDATE_VERIFY_TIMEOUT=240 ./start.sh update --yes || fail "the update from the mirror"
expect "version" "$(version)" "$V3"
expect "migration" "$(sql 'select version_num from alembic_version')" 99aa00000002
expect "test tables" "$(tables)" "upd_test_b,upd_test_c,upd_test_marker"
expect "marker" "$(sql 'select note from upd_test_marker')" "$marker"
expect "history" "$(tail -1 data/updates/history.log | awk '{print $2, $3, $4}')" "$V1 $V3 succeeded"

step "upgrade test passed"
