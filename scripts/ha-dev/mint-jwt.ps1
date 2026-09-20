# Print a short-lived access JWT for a local user, for API smoke checks.
# Signed inside the core container with its own SECRET_KEY (never read here).
#
# Usage:  $t = scripts/ha-dev/mint-jwt.ps1 [-User admin] [-Minutes 30]
param([string]$User = 'admin', [int]$Minutes = 30)

$py = @"
import os, time, uuid
from jose import jwt
now = int(time.time())
claims = {'sub': '$User', 'type': 'access', 'iat': now, 'nbf': now,
          'exp': now + $Minutes * 60, 'jti': uuid.uuid4().hex}
print(jwt.encode(claims, os.environ['SECRET_KEY'], algorithm='HS256'))
"@
docker exec opennvr_core /app/server-venv/bin/python -c $py
