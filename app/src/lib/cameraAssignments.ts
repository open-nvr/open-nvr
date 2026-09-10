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
 * Which cameras an app may be offered, and which it actually runs on.
 *
 * The browser mirror of `server/services/skill_assignments.py`. Keep the
 * two in step: this decides what a picker shows, the server decides what
 * computes, and a picker that offers a camera the server will refuse is
 * worse than no picker at all.
 *
 *   eligible — no camera is claimed by anyone, or this skill is among
 *              its claims. What a picker may OFFER.
 *   adopted  — this skill is among the camera's claims. What COSTS
 *              money: skill inference runs only on adopted cameras.
 *
 * An unassigned camera is eligible everywhere and adopted nowhere, so a
 * fresh install shows a full picker and pays for nothing until an
 * operator points a skill at something.
 */

export const LPR_SKILL = 'license_plate_recognition'

export type CameraAssignment = { skill?: string | null; label?: string | null }

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

/** May this skill be offered this camera? Unclaimed cameras are open. */
export function cameraEligible(
  camera: { assignments?: CameraAssignment[] | null },
  skill: string
): boolean {
  const claimed = cameraSkills(camera)
  return claimed.length === 0 || claimed.includes(skill.trim().toLowerCase())
}

/**
 * Why a camera is not on offer, phrased for an operator who is looking
 * for it and cannot find it. Null when it is eligible.
 */
export function ineligibleReason(
  camera: { assignments?: CameraAssignment[] | null },
  skill: string
): string | null {
  if (cameraEligible(camera, skill)) return null
  const other = cameraSkills(camera).map(prettySkill).join(', ')
  return `assigned to ${other}`
}

export function prettySkill(skill: string): string {
  return skill.replace(/[_-]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}
