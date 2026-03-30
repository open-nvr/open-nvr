# Utilities Module

Reusable helper functions for encryption and Docker path translation.

---

## 📁 **Files**

### **`encryption.py`**
**Symmetric encryption for sensitive data (camera passwords, API tokens)**

**Key Class:**
- `EncryptionManager` - Fernet symmetric encryption service

**Helper Functions:**
```python
from utils.encryption import encrypt_value, decrypt_value

# Encrypt before saving to database
encrypted = encrypt_value("camera_password")

# Decrypt when needed
password = decrypt_value(camera.encrypted_password)
```

**Configuration:**
- Requires `CREDENTIAL_ENCRYPTION_KEY` in `server/.env`
- Generate with: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`

**Important:**
- ⚠️ Never commit encryption key to git
- ⚠️ Key loss = data loss (no recovery)
- ⚠️ Never log decrypted values

---

### **`path_mapper.py`**
**Convert paths between host and Docker container filesystems**

**Problem it solves:**
- Host path: `D:/opennvr/Recordings/cam-1/video.mp4`
- Container path: `/app/recordings/cam-1/video.mp4`
- Backend needs to translate when configuring MediaMTX

**Functions:**

**`host_to_container_path(host_path: str) -> str`**
```python
from utils.path_mapper import host_to_container_path

host = "D:/opennvr/Recordings/cam-1/video.mp4"
container = host_to_container_path(host)
# Returns: "/app/recordings/cam-1/video.mp4"
```

**`container_to_host_path(container_path: str) -> str`**
```python
from utils.path_mapper import container_to_host_path

container = "/app/recordings/cam-1/video.mp4"
host = container_to_host_path(container)
# Returns: "D:/opennvr/Recordings/cam-1/video.mp4"
```

**`get_mediamtx_recording_path(user_configured_path: str = None) -> str`**
```python
# Get MediaMTX recording path (container context)
mtx_path = get_mediamtx_recording_path("D:/custom-recordings")
# Returns: "/app/recordings" (for MediaMTX config)
```

**Configuration:**
```env
# In server/.env
RECORDINGS_HOST_BASE=D:/opennvr/Recordings      # Host filesystem
RECORDINGS_CONTAINER_BASE=/app/recordings          # Container mount point
```

**Important:**
- ✅ Must match `docker-compose.yml` volume mounts exactly
- ✅ Handles both Windows and Unix paths
- ⚠️ Mismatched config = file not found errors

---

## 🔧 **Usage Examples**

**Encrypt camera credentials:**
```python
from utils.encryption import encrypt_value, decrypt_value

camera = Camera(
    name="Front Door",
    username="admin",
    encrypted_password=encrypt_value("camera_pass_123")
)
db.add(camera)

# Later, when connecting to camera
actual_password = decrypt_value(camera.encrypted_password)
```

**Configure MediaMTX recording path:**
```python
from utils.path_mapper import host_to_container_path

user_path = "D:/opennvr/Recordings"  # From user config
container_path = host_to_container_path(user_path)

await MediaMtxAdminService.pathdefaults_patch({
    "recordPath": f"{container_path}/%path/%Y/%m/%d/%H-%M-%S-%f"
})
```

---

## ⚠️ **Important Notes**

**Encryption:**
- Uses Fernet (symmetric encryption from `cryptography` library)
- Singleton pattern - one manager instance per application
- Empty strings remain empty (no encryption)

**Path Mapping:**
- Cross-platform support (Windows ↔ Linux)
- Normalizes path separators and case
- Only needed when running in Docker containers
