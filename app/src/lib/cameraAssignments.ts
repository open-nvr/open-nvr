/**
 * Copyright (c) 2026 OpenNVR
 * This file is part of OpenNVR.
 *
 * OpenNVR is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * OpenNVR is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU Affero General Public License
 * along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.
 */

/**
 * What a camera computes — the browser mirror of
 * `server/services/skill_assignments.py`.
 *
 *   adopted — this skill is among the camera's claims. What COSTS money:
 *             skill inference runs only on adopted cameras. An app's
 *             pick of a camera is a claim, so a picked camera is adopted
 *             for that app's skill.
 *
 * There is no "eligible" any more. Every camera is available to every
 * app, and any number of apps may pick the same one; the helper that used
 * to grey out a camera another skill had claimed is gone with that rule.
 */

export const LPR_SKILL = 'license_plate_recognition'

/** One entry of a camera's projected assignments (the server sends
 *  `labels`, a list, when a claim narrows detection). */
export type CameraAssignment = { skill?: string | null; labels?: string[] | null }

/** The skills a camera carries, lower-cased and trimmed. */
export function cameraSkills(camera: { assignments?: CameraAssignment[] | null }): string[] {
  const entries = camera?.assignments
  if (!Array.isArray(entries)) return []
  const out: string[] = []
  for (const entry of entries) {
    const skill = typeof entry?.skill === 'string' ? entry.skill.trim().toLowerCase() : ''
    if (skill && !out.includes(skill)) out.push(skill)
  }
  return out
}

/** Does this camera carry the skill — i.e. does its inference run? */
export function cameraAdopted(
  camera: { assignments?: CameraAssignment[] | null },
  skill: string
): boolean {
  return cameraSkills(camera).includes(skill.trim().toLowerCase())
}

export function prettySkill(skill: string): string {
  return skill.replace(/[_-]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}
