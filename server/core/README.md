# Core Module

Foundation modules for the OpenNVR backend application. This directory contains framework-level utilities that are used across the entire application.

---

## 📁 **Module Files**

### **`config.py`**
**Application configuration and environment variable management**

- Loads all settings from `.env` file using Pydantic Settings
- Single source of truth for application configuration
- Includes database, security, MediaMTX, recording, and AI settings
- Field validators for data validation
- Default values with environment variable overrides

**Usage:**
```python
from core.config import settings

print(settings.database_url)
print(settings.mediamtx_secret)
```

**Key Configuration Areas:**
- Database connection (`DATABASE_URL`)
- JWT authentication (`SECRET_KEY`, `ACCESS_TOKEN_EXPIRE_MINUTES`)
- MediaMTX integration (URLs, tokens, webhook secrets)
- Recording paths and storage settings
- CORS origins and API settings
- AI inference configuration

---

### **`database.py`**
**SQLAlchemy database connection and session management**

- Creates PostgreSQL connection with optimized pooling
- Provides database session dependency for FastAPI routes
- Manages connection lifecycle (open/close/cleanup)
- Auto-runs Alembic migrations on startup

**Connection Pool Settings:**
- 20 persistent connections
- 10 overflow connections (total: 30)
- Connection pre-ping validation
- 5-minute connection recycling

**Usage:**
```python
from core.database import get_db
from sqlalchemy.orm import Session

@router.get("/users")
def get_users(db: Session = Depends(get_db)):
    return db.query(User).all()
```

---

### **`auth.py`**
**Authentication and authorization utilities**

- Password hashing with bcrypt
- JWT token creation and validation
- FastAPI dependencies for protected routes
- 2FA/TOTP support
- Session management and token blacklisting

**Key Functions:**
- `verify_password()` - Bcrypt password verification
- `get_password_hash()` - Generate password hash
- `create_access_token()` - Generate JWT token with enhanced claims
- `decode_access_token()` - Validate and decode JWT

**FastAPI Dependencies:**
- `get_current_user()` - Extract user from JWT token
- `get_current_active_user()` - Ensure user is active (not disabled)
- `get_current_superuser()` - Require admin privileges

**Usage:**
```python
from core.auth import get_current_active_user

@router.get("/me")
def read_me(current_user: User = Depends(get_current_active_user)):
    return current_user
```

---

### **`permissions.py`**
**Resource ownership and permission checking**

- Generic permission checker for any database model
- Validates resource existence (404 if not found)
- Validates user ownership (403 if unauthorized)
- Superuser bypass for all checks

**Classes:**
- `PermissionChecker` - Generic ownership validator (configurable model and field)

**Built-in Dependencies:**
- `get_camera_or_403()` - Camera ownership validation

**Usage:**
```python
from core.permissions import get_camera_or_403

@router.get("/cameras/{camera_id}")
def get_camera_detail(camera: Camera = Depends(get_camera_or_403)):
    # camera is already validated - user owns it or is superuser
    return camera
```

**Creating Custom Permission Checkers:**
```python
from core.permissions import PermissionChecker
from models import Recording

# Create dependency for recordings
def get_recording_or_403(
    recording_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
) -> Recording:
    checker = PermissionChecker(Recording, ownership_field="user_id")
    return checker.check(recording_id, current_user, db)
```

---

### **`logging_config.py`**
**Centralized logging configuration with structured output**

- JSON-formatted logs for machine parsing
- File rotation (50MB per file, 10 backups)
- Multiple specialized loggers
- Request correlation IDs
- Contextual metadata enrichment

**Available Loggers:**
- `main_logger` - General application events
- `auth_logger` - Authentication/authorization events
- `request_logger` - HTTP request/response logs
- `camera_logger` - Camera operations
- `recording_logger` - Recording events
- `ai_logger` - AI inference logs
- `security_logger` - Security events (intrusion, violations)
- `audit_logger` - Audit trail for compliance

**Log File Location:** `logs/server.log` (auto-rotated)

**Usage:**
```python
from core.logging_config import camera_logger

camera_logger.info("Camera created", extra={
    'user_id': user.id,
    'camera_id': camera.id,
    'action': 'create',
    'ip_address': request.client.host
})
```

**Log Format (JSON):**
```json
{
  "timestamp": "2026-02-23T10:30:45.123Z",
  "level": "INFO",
  "logger": "camera",
  "message": "Camera created",
  "module": "cameras",
  "function": "create_camera",
  "line": 145,
  "user_id": 1,
  "camera_id": 5,
  "action": "create",
  "ip_address": "192.168.1.100"
}
```

---

## 🏗️ **Architecture Pattern**

This module follows the **Dependency Injection** pattern used throughout the FastAPI application:

```
main.py (FastAPI app)
    ↓
routers/ (API endpoints)
    ↓
core/ (shared utilities) ←── YOU ARE HERE
    ├── config.py       → Application settings
    ├── database.py     → DB session management
    ├── auth.py         → User authentication
    ├── permissions.py  → Authorization checks
    └── logging_config  → Structured logging
    ↓
models.py (SQLAlchemy ORM models)
    ↓
schemas.py (Pydantic validation)
```

---

## 🔐 **Security Considerations**

- **Passwords:** Never stored in plain text (bcrypt hashing)
- **Secrets:** Loaded from environment variables (not hardcoded)
- **JWT Tokens:** Include jti (token ID) for revocation support
- **Timing Attacks:** Password verification uses constant-time comparison
- **Session Management:** Token expiration and blacklisting
- **Permission Checks:** Default-deny approach (explicit ownership required)

---

## 🧪 **Testing**

When writing tests that use core modules:

```python
# Override config for testing
from core.config import Settings

test_settings = Settings(
    database_url="sqlite:///test.db",
    secret_key="test-secret-key",
    # ... other settings
)

# Override database for testing
from core.database import Base, engine
from sqlalchemy import create_engine

test_engine = create_engine("sqlite:///test.db")
Base.metadata.create_all(bind=test_engine)
```

---

## 📝 **Best Practices**

1. **Configuration:** Always use `settings` from `core.config` - never hardcode values
2. **Database Sessions:** Always use `Depends(get_db)` - never create sessions directly
3. **Authentication:** Use appropriate dependency (`get_current_user()`, `get_current_active_user()`, `get_current_superuser()`)
4. **Permissions:** Use `PermissionChecker` for resource ownership validation
5. **Logging:** Use specialized loggers (`camera_logger`, `auth_logger`, etc.) with contextual metadata

---

## 🔄 **Import Conventions**

```python
# Configuration
from core.config import settings

# Database
from core.database import get_db

# Authentication
from core.auth import (
    get_current_user,
    get_current_active_user,
    get_current_superuser,
    create_access_token
)

# Permissions
from core.permissions import get_camera_or_403, PermissionChecker

# Logging
from core.logging_config import camera_logger, auth_logger
```

---

## 📚 **Related Documentation**

- [Local Setup Guide](../../docs/LOCAL_SETUP.md) - Environment configuration
- [API Documentation](../README.md) - Backend API overview
- [Database Migrations](../migrations/README.md) - Alembic migration guide
