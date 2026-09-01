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

"""Durable adapter-registration state (issue #371).

The registry itself stays an in-memory cache — this module is the thin
receipt file underneath it, so that adapters registered at RUNTIME (an
app overlay's one-shot registrar, an operator on the AI Models page)
survive a KAI-C restart. Before this file existed, restarting
``opennvr-core`` silently forgot every runtime registration: the
one-shot registrar had already exited, nothing re-registered the
adapter, and every plate read 404'd with no operator signal.

Design constraints, in order:

* **Never block or break boot.** A missing, unreadable, or corrupt
  state file degrades to "no persisted adapters" with a WARNING —
  exactly the pre-#371 behaviour, never a crash loop.
* **Persist intent, not state.** We store (name, url, granted
  permission keys) — the operator's decisions. Capabilities, health,
  and fingerprints are re-fetched from the live adapter on restore, so
  the §11.3 drift machinery (not a stale snapshot) decides what the
  adapter looks like today. A restored grant is re-applied through the
  normal ``grant_permissions`` path, which intersects against the
  freshly-declared key set: a key the adapter no longer declares is
  dropped, a key it newly declares stays pending. No approval
  side-channel.
* **Atomic writes.** tmp + ``os.replace`` so a power cut mid-write
  leaves the previous file, not half a JSON document.

Only runtime-registered adapters are persisted. Seeds from
``ADAPTER_REGISTRY`` are re-registered from configuration on every
boot (config-as-consent, §8.5) — persisting them too would let a
seed the operator *removed from config* resurrect from disk.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

STATE_FILE_NAME = "adapters.json"
STATE_VERSION = 1


class RegistryStateStore:
    """Load/save the runtime-adapter receipt file.

    ``directory=None`` disables persistence entirely (unit tests, or an
    operator explicitly opting out with ``KAI_C_STATE_DIR=""``) — every
    method becomes a no-op that reports "nothing persisted".
    """

    def __init__(self, directory: str | Path | None) -> None:
        self._path: Path | None = None
        if directory:
            self._path = Path(directory) / STATE_FILE_NAME

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def enabled(self) -> bool:
        return self._path is not None

    def load(self) -> list[dict[str, Any]]:
        """Return the persisted adapter entries — ``[{name, url,
        granted_permissions}]`` — or ``[]`` for missing/disabled/corrupt
        state. Never raises: a broken file is a WARNING and an empty
        list, because failing boot over a receipt file would be worse
        than the amnesia it fixes."""
        if self._path is None or not self._path.exists():
            return []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning(
                "adapter state file %s is unreadable (%s) — starting with "
                "no persisted adapters; runtime registrations will "
                "re-persist as they arrive", self._path, exc,
            )
            return []
        entries = raw.get("adapters") if isinstance(raw, dict) else None
        if not isinstance(entries, list):
            logger.warning(
                "adapter state file %s has an unexpected shape — ignoring it",
                self._path,
            )
            return []
        out: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            url = entry.get("url")
            if not isinstance(name, str) or not name:
                continue
            if not isinstance(url, str) or not url:
                continue
            granted = entry.get("granted_permissions")
            keys = [k for k in granted if isinstance(k, str)] \
                if isinstance(granted, list) else []
            out.append({
                "name": name,
                "url": url,
                "granted_permissions": sorted(set(keys)),
            })
        return out

    def save(self, entries: list[dict[str, Any]]) -> None:
        """Atomically write the entries. Best-effort: an unwritable
        state dir is a WARNING (once per process would be nicer, but
        writes are rare — one per registration/grant), never an
        exception into the registration path."""
        if self._path is None:
            return
        payload = {"version": STATE_VERSION, "adapters": entries}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=str(self._path.parent), prefix=".adapters-", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2, sort_keys=True)
                os.replace(tmp, self._path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            logger.warning(
                "could not persist adapter state to %s: %s — runtime "
                "registrations will NOT survive a restart until this is "
                "fixed", self._path, exc,
            )
