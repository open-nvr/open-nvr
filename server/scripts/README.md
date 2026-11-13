# Server Scripts

Utility scripts for database initialization, user management, and administrative tasks.

---

## 📁 **Available Scripts**

### **`init_db.py`**
**Database initialization and setup**

**Purpose:** Creates database tables, initial roles, permissions, and default admin user.

**When to use:**
- ✅ First-time setup (after `alembic upgrade head`)
- ✅ Resetting database with fresh roles/permissions
- ✅ Adding default admin user if missing

**What it creates:**

#### **Roles:**
- `admin` - Full access to all features
- `operator` - Camera management, live view, recordings
- `viewer` - Read-only access (cameras, live view, playback)

#### **Permissions:** (30+ permissions)
- User management (`users.view`, `users.manage`)
- Role management (`roles.view`, `roles.manage`)
- Camera operations (`cameras.view`, `cameras.manage`)
- Live streaming (`live.view`)
- Recordings (`recordings.view`, `recordings.manage`)
- Settings (`settings.view`, `settings.manage`)
- Network configuration
- ONVIF discovery
- AI engine configuration
- Compliance reports
- And more...

#### **Admin User:**
- Username: From `DEFAULT_ADMIN_USERNAME` in `.env` (default: `admin`)
- Password: From `DEFAULT_ADMIN_PASSWORD` in `.env` (default: `admin`)
- Email: From `DEFAULT_ADMIN_EMAIL` in `.env`
- Role: Admin with superuser privileges

**Usage:**
```bash
# From server/ directory
cd server
python scripts/init_db.py
```

**Output:**
```
Initializing database...
Database tables created successfully
Created admin user: admin
IMPORTANT: Change the admin password after first login!
Roles present: admin, operator, viewer
Permissions present: ai.manage, ai.view, alerts.manage, ...
Database initialization completed!
```

**Important Notes:**
- ⚠️ **Change default admin password immediately** after first login
- ✅ Safe to run multiple times (idempotent - won't duplicate data)
- ✅ Automatically assigns all permissions to admin role
- ✅ Creates missing roles/permissions without deleting existing ones

**Environment Variables Required:**
```env
# In server/.env
DEFAULT_ADMIN_USERNAME=admin
DEFAULT_ADMIN_PASSWORD=changeme123  # CHANGE THIS!
DEFAULT_ADMIN_EMAIL=admin@example.com
DEFAULT_ADMIN_FIRST_NAME=System
DEFAULT_ADMIN_LAST_NAME=Administrator
```

---

### **`disable_mfa.py`**
**Reset a user's two-factor sign-in from the host**

**Purpose:** Recovery when nobody can use the web UI's **Settings → Users → Reset 2FA** action, typically the only administrator having lost their authenticator. For any other user, prefer the UI action: it needs the administrator's own code and e-mails the user.

**Usage (Docker install):**
```bash
docker exec -it opennvr_core python /app/server/scripts/disable_mfa.py <username>
```

The username is required; the script never picks one for you.

**What it does** (the same `services.mfa_service.reset_mfa` the UI action calls):
1. Removes the user's TOTP secret and sets `mfa_enabled = False`
2. Ends every browser session of theirs (bumps `token_version`; `onvr_` API tokens are not touched)
3. Writes an `mfa.cli_reset` audit entry
4. A single sign-on account whose identity provider owns MFA is refused: there is nothing to reset

**Afterwards:** at their next password sign-in the user is taken straight to the enrolment screen and scans a new QR code. Until they enrol, that session reaches nothing else.

**Security Note:**
- ⚠️ Until the user enrols again, their password alone gets them to the enrolment screen. Run it when they are ready to sign in and enrol, or reset their password at the same time.
- ⚠️ Anyone who can run `docker exec` on the host already controls OpenNVR; this script gives them nothing new.

---

## 🗂️ **Subdirectories**

### **`debug_tools/`**
**Developer debugging utilities (currently empty)**

Reserved for future debugging scripts like:
- Database connection testing
- Model inspection tools
- Performance profiling scripts
- Data validation utilities

---

## 🚀 **Common Workflows**

### **Initial Project Setup**
```bash
# 1. Run database migrations
cd server
alembic upgrade head

# 2. Initialize database with roles & admin user
python scripts/init_db.py

# 3. Start server
uvicorn main:app --reload
```

---

### **User Locked Out (MFA Issues)**
```bash
# User calls support: "I lost my phone with Google Authenticator"

# Preferred: Settings → Users → Reset 2FA (needs your own code).
# No administrator available? On the host:
docker exec -it opennvr_core python /app/server/scripts/disable_mfa.py johndoe

# Then the user signs in with username/password and is taken straight
# to the enrolment screen to scan a new QR code.
```

---

### **Reset Admin Password**
```bash
# If admin forgot password, two options:

# Option 1: Use disable_mfa.py to access, then change via UI
python scripts/disable_mfa.py admin
# Login with old password, change in settings

# Option 2: Direct database update (advanced)
# Create a new script: reset_password.py
# Or use database client to update hashed_password
```

---

## 🛡️ **Security Considerations**

### **init_db.py**
- ✅ Default admin password should be changed immediately
- ✅ Store admin credentials in `.env` file (gitignored)
- ⚠️ Never commit `.env` with production passwords
- 📝 Audit logs track admin user creation

### **disable_mfa.py**
- ⚠️ **Requires shell access to the host** - only for administrators
- ✅ The user must enrol a new authenticator at their next sign-in
- 📝 Recorded in the audit log as `mfa.cli_reset`

---

## 🧪 **Testing**

### **Test init_db.py**
```bash
# 1. Fresh database
dropdb opennvr
createdb opennvr

# 2. Run migrations
alembic upgrade head

# 3. Initialize (should succeed)
python scripts/init_db.py

# 4. Run again (should be idempotent - no errors)
python scripts/init_db.py

# 5. Verify admin user exists
psql opennvr -c "SELECT username, is_superuser FROM users WHERE username='admin';"
```

### **Test disable_mfa.py**
```bash
# 1. Enable MFA for test user via UI
# 2. Verify MFA is required for login
# 3. Run script
python scripts/disable_mfa.py testuser

# 4. Verify login with the password lands on the enrolment screen
# 5. Check database
psql opennvr -c "SELECT username, mfa_enabled, token_version FROM users WHERE username='testuser';"
```

---

## 📝 **Script Template**

When adding new scripts to this directory, follow this template:

```python
import sys, os; sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
"""
Brief description of what this script does.
"""

from core.database import SessionLocal
from models import YourModel
from core.config import settings


def main():
    """Main function."""
    db = SessionLocal()
    try:
        # Your logic here
        pass
    except Exception as e:
        print(f"❌ Error: {e}")
        db.rollback()
    finally:
        db.close()


if __name__ == "__main__":
    main()
```

**Key elements:**
1. Path setup (allows importing from `core/`, `models.py`)
2. Docstring explaining purpose
3. Database session management (open → try/except → close)
4. Error handling with rollback
5. `if __name__ == "__main__"` guard

---

## 🔄 **Script Dependencies**

All scripts require:
- ✅ PostgreSQL database running
- ✅ `server/.env` configured with `DATABASE_URL`
- ✅ Virtual environment activated
- ✅ Run from `server/` directory

```bash
# Activate environment
cd server
source venv/bin/activate  # Linux/Mac
.\venv\Scripts\activate   # Windows

# Run script
python scripts/<script_name>.py
```

---

## 📚 **Related Documentation**

- [Database Migrations](../migrations/README.md) - Alembic migration guide
- [Core Module](../core/README.md) - Database connection, auth utilities
- [Local Setup](../../docs/LOCAL_SETUP.md) - Initial project setup

---

## 🔮 **Future Scripts** (Planned)

Potential utility scripts to add:

| Script | Purpose | Priority |
|--------|---------|----------|
| `reset_password.py` | Reset user password without login | Medium |
| `list_users.py` | Display all users with roles | Low |
| `backup_db.py` | Create database backup | Medium |
| `verify_permissions.py` | Audit role permissions | Low |
| `cleanup_old_recordings.py` | Delete recordings older than X days | Medium |
| `generate_api_token.py` | Create long-lived API token for integrations | Low |

---

## ⚠️ **Important Reminders**

1. **Always run from `server/` directory** - Scripts expect project root in path
2. **Database must be running** - Scripts connect to PostgreSQL
3. **Use with caution in production** - These are administrative tools
4. **Backup before running** - Especially if modifying user data
5. **Check exit codes** - Scripts indicate success/failure

---

## 💡 **Tips**

### **Adding New Permissions**
Edit `init_db.py`, add to permission definitions, then run:
```bash
python scripts/init_db.py  # Will add new permissions without affecting existing
```

### **Creating Custom Admin User**
Modify `server/.env`:
```env
DEFAULT_ADMIN_USERNAME=superadmin
DEFAULT_ADMIN_PASSWORD=MySecurePassword123!
DEFAULT_ADMIN_EMAIL=admin@yourcompany.com
```

Then run:
```bash
python scripts/init_db.py
```

### **Emergency Database Access**
If all else fails, use direct SQL:
```bash
psql opennvr

-- Disable MFA for all users
UPDATE users SET mfa_enabled = false, mfa_secret = NULL;

-- Reset admin password (hash of 'admin')
UPDATE users SET hashed_password = '$2b$12$...' WHERE username = 'admin';
```

**But prefer using scripts when possible!** They have proper error handling and validation.
