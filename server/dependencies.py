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
Common dependencies for the FastAPI application.
Provides reusable dependency functions to avoid circular imports.
"""

from core.auth import get_current_active_user, get_current_superuser, get_current_user

# Re-export dependencies to avoid circular imports
get_current_active_user = get_current_active_user
get_current_user = get_current_user
get_current_superuser = get_current_superuser
