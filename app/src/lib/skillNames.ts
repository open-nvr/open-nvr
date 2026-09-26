// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Model skills (task ids) in the words an operator uses, shared by every
// page that has to say what an app needs or adds.

/** A skill id said the way an operator would say it. Ids are the model
 *  task names (server/config/tasks.yml); an unknown one falls back to its
 *  id with spaces, which is still readable. */
const SKILL_NAMES: Record<string, string> = {
  embed: 'smart search',
  face_detection: 'face detection',
  face_recognition: 'face recognition',
  image_captioning: 'scene descriptions',
  license_plate_recognition: 'number plate reading',
  multi_object_tracking: 'object tracking',
  object_detection: 'object detection',
  package_detection: 'package detection',
  person_detection: 'person detection',
  pose_estimation: 'pose detection',
  speech_to_text: 'speech to text',
  text_to_speech: 'text to speech',
  vqa: 'visual questions',
}
export function skillLabel(id: string): string {
  return SKILL_NAMES[id] ?? id.replace(/_/g, ' ')
}

/** "a", "a and b", "a, b and c". */
export function listOf(items: string[]): string {
  return items.length < 2 ? (items[0] ?? '') : `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`
}
