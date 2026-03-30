# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
# 
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""
KAI-C Startup Script

Run this to start the KAI-C HTTP service.
"""

import uvicorn

if __name__ == "__main__":
    print("=" * 70)
    print("Starting KAI-C (Kavach AI Connector) HTTP Service")
    print("=" * 70)
    print()
    print("Server URL: http://localhost:8100")
    print("API Documentation: http://localhost:8100/docs")
    print("Health Check: http://localhost:8100/health")
    print()
    print("Architecture:")
    print("  Frontend -> Backend -> KAI-C (Port 8100) -> AI Adapter (Port 9100)")
    print()
    print("=" * 70)
    print()
    
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8100,
        reload=True,
        log_level="info"
    )
