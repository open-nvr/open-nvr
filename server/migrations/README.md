# Database Migrations Guide

This project uses **Alembic** for database schema migrations with PostgreSQL.

## Quick Reference

```powershell
cd server

# Apply all pending migrations
alembic upgrade head

# Check current migration status
alembic current

# View migration history
alembic history

# See pending migrations
alembic history --indicate-current
```

---

## Common Commands

### Apply Migrations

```powershell
# Apply all pending migrations
alembic upgrade head

# Apply next migration only
alembic upgrade +1

# Upgrade to specific revision
alembic upgrade a1b2c3d4e5f6
```

### Rollback Migrations

```powershell
# Rollback last migration
alembic downgrade -1

# Rollback to specific revision
alembic downgrade 449ffb3373b2

# Rollback all migrations (DANGER!)
alembic downgrade base
```

### Check Status

```powershell
# Show current revision
alembic current

# Show all migrations with current marked
alembic history --indicate-current

# Show verbose history
alembic history -v

# Show all head revisions
alembic heads
```

---

## Creating New Migrations

### Option 1: Auto-generate from model changes (recommended)

After modifying `models.py`:

```powershell
alembic revision --autogenerate -m "description of changes"
```

### Option 2: Create empty migration manually

```powershell
alembic revision -m "description of changes"
```

Then edit the generated file in `migrations/versions/`.

---

## Migration File Structure

Each migration file in `migrations/versions/` has:

```python
revision = 'a1b2c3d4e5f6'      # This migration's unique ID
down_revision = '449ffb3373b2'  # Previous migration's ID (parent)

def upgrade():
    # Changes to apply
    op.add_column('table', sa.Column('name', sa.String(100)))

def downgrade():
    # How to reverse the changes
    op.drop_column('table', 'name')
```

---

## Troubleshooting

### Multiple heads error

```
ERROR: Multiple head revisions are present
```

**Fix:**
```powershell
# See the conflicting heads
alembic heads

# Merge them
alembic merge heads -m "merge migration branches"

# Then upgrade
alembic upgrade head
```

### Migration already applied but file changed

```powershell
# Mark migration as current without running it
alembic stamp head
```

### Database out of sync

```powershell
# Check what revision DB thinks it's at
alembic current

# Force stamp to specific revision
alembic stamp a1b2c3d4e5f6
```

### View SQL without executing

```powershell
# Show SQL for upgrade
alembic upgrade head --sql

# Show SQL for downgrade
alembic downgrade -1 --sql
```

---

## Migration History

| Revision | Description | Date |
|----------|-------------|------|
| `5a14a1dba41d` | Add RTSP proxy fields to camera_config | 2025-09-19 |
| `449ffb3373b2` | Remove IP address uniqueness constraint | 2025-09-27 |
| `a1b2c3d4e5f6` | Add ONVIF metadata fields to cameras | 2025-12-08 |

---

## After Pulling Changes

If new migrations were added by teammates:

```powershell
cd server
alembic upgrade head
```

---

## Best Practices

1. **Always backup before migrating production**
2. **Test migrations locally first**
3. **Never edit migrations that are already applied in production**
4. **Use descriptive migration messages**
5. **Review auto-generated migrations before applying**
