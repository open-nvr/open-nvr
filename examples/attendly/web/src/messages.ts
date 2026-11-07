// Centralized user-facing messages

export const messages = {
  common: {
    trialFeatureDisabled: 'This option is not available in the trial version.',
    ok: 'OK',
    cancel: 'Cancel',
    delete: 'Delete',
    confirm: 'Confirm',
  },
  attendance: {
    exportPdfFailed: 'Failed to export PDF',
    exportExcelFailed: 'Failed to export Excel',
  },
  registry: {
    studentFacesTitle: (studentName: string) => `Student Faces — ${studentName}`,
    studentFacesInfo: (studentName: string, totalFaces: number, faceIds: string[]) => (
      `Student: ${studentName}\nTotal Faces: ${totalFaces}\n\nFace IDs:\n${faceIds.join('\n')}`
    ),
    failedLoadStudentFaces: 'Failed to load student faces',
    confirmDeleteStudentFaces: (studentCode: string) => `Are you sure you want to delete all faces for student ${studentCode}?`,
    deletedStudentFaces: (count: number, studentCode: string) => `Deleted ${count} faces for student ${studentCode}`,
    failedDeleteStudentFaces: 'Failed to delete student faces',
  },
  streams: {
    publishTodaySuccess: "Published today's schedules to classrooms.",
    publishTodayFailed: (detail?: string) => String(detail || 'Failed to publish'),
    roomNameRequired: 'Room name is required.',
    cameraRequired: 'Pick one of the OpenNVR cameras.',
    failedAddRoom: (detail?: string) => `Failed to add room: ${detail || 'Unknown error'}`,
    courseCodeRequired: 'Course code is required.',
    failedAddCourse: (detail?: string) => `Failed to add course: ${detail || 'Unknown error'}`,
    failedRemoveCourse: (detail?: string) => `Failed to remove course: ${detail || 'Unknown error'}`,
    failedDeleteSchedule: (detail?: string) => `Failed to delete schedule: ${detail || 'Unknown error'}`,
    failedCreateCourse: (detail?: string) => String(detail || 'Failed to create course'),
  },
  manage: {
    confirmDeleteSchedule: 'Delete this schedule?',
    confirmDeleteRoom: 'Delete this room?',
    confirmDeleteCourse: 'Delete this course?',
  },
}

export type ToastType = 'success' | 'error' | 'info'


