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

import React, { useState, useEffect } from 'react';
import { apiService } from '../lib/apiService';
import { RecordingTimeline } from './RecordingTimeline';
import { useTranslation, useDateFormat } from '../i18n';
import { useSnackbar } from './Snackbar';

interface RecordingSegment {
  path: string;
  start_time: string;
  end_time: string;
  duration_seconds: number;
  size_bytes: number;
  is_complete: boolean;
}

interface RecordingSession {
  session_id: string;
  start_time: string;
  end_time: string;
  duration_seconds: number;
  duration_formatted: string;
  size_bytes: number;
  size_formatted: string;
  segment_count: number;
  complete_segment_count: number;
  incomplete_segment_count: number;
  is_in_progress: boolean;
  complete_duration_seconds: number;
  complete_duration_formatted: string;
  segments: RecordingSegment[];
}

interface RecordingDate {
  date: string;
  session_count: number;
  total_duration_seconds: number;
  sessions: RecordingSession[];
}

interface CameraRecordings {
  camera_id: number;
  camera_name: string;
  camera_location?: string;
  dates: RecordingDate[];
}

interface RecordingBrowserProps {
  cameras: { id: number; name: string }[];
  onSelect: (data: {
    camera_id: number;
    session_id: string;
    segments: string[];
    start_time?: string;
    end_time?: string;
  }) => void;
  onClose: () => void;
}

export const RecordingBrowser: React.FC<RecordingBrowserProps> = ({
  cameras,
  onSelect,
  onClose
}) => {
  const { t } = useTranslation();
  const fmt = useDateFormat();
  const { showWarning } = useSnackbar();
  const [loading, setLoading] = useState(false);
  const [cameraRecordings, setCameraRecordings] = useState<CameraRecordings[]>([]);
  const [selectedCameraId, setSelectedCameraId] = useState<number | null>(null);
  const [expandedDates, setExpandedDates] = useState<Set<string>>(new Set());
  const [selectedSession, setSelectedSession] = useState<RecordingSession | null>(null);
  const [selectedCameraForSession, setSelectedCameraForSession] = useState<number | null>(null);
  const [timeRange, setTimeRange] = useState<{ start: string; end: string } | null>(null);

  useEffect(() => {
    loadRecordingSessions();
  }, []);

  const loadRecordingSessions = async (cameraId?: number) => {
    setLoading(true);
    try {
      const params: { camera_id?: number } = {};
      if (cameraId) {
        params.camera_id = cameraId;
      }
      
      const response = await apiService.getRecordingSessionsForAI(params);
      setCameraRecordings(response.data.cameras || []);
    } catch (error) {
      console.error('Failed to load recording sessions:', error);
    } finally {
      setLoading(false);
    }
  };

  const toggleDate = (cameraId: number, date: string) => {
    const key = `${cameraId}-${date}`;
    const newExpanded = new Set(expandedDates);
    if (newExpanded.has(key)) {
      newExpanded.delete(key);
    } else {
      newExpanded.add(key);
    }
    setExpandedDates(newExpanded);
  };

  const handleSessionClick = (session: RecordingSession, cameraId: number) => {
    setSelectedSession(session);
    setSelectedCameraForSession(cameraId);
    setTimeRange(null); // Reset time range when session changes
  };

  const handleAnalyze = () => {
    if (!selectedSession || selectedCameraForSession === null) return;

    // Filter to only complete segments (exclude segments still being written)
    const completeSegments = selectedSession.segments.filter(s => s.is_complete);
    const segmentPaths = completeSegments.map(s => s.path);
    
    console.log('handleAnalyze - selectedSession:', selectedSession);
    console.log('handleAnalyze - complete segments:', completeSegments.length, 'of', selectedSession.segments.length);
    console.log('handleAnalyze - segment paths:', segmentPaths);
    console.log('handleAnalyze - timeRange:', timeRange);

    // Validate that we have valid segment paths
    const validSegmentPaths = segmentPaths.filter(path => path && path.trim() !== '');
    
    if (validSegmentPaths.length === 0) {
      showWarning('No complete segments available for analysis. Wait for at least one segment to complete, or select a different recording.');
      return;
    }

    const data = {
      camera_id: selectedCameraForSession,
      session_id: selectedSession.session_id,
      segments: validSegmentPaths, // Use only complete, valid paths
      start_time: timeRange?.start,
      end_time: timeRange?.end
    };
    
    console.log('handleAnalyze - calling onSelect with:', data);
    onSelect(data);
  };

  const handleBackToList = () => {
    setSelectedSession(null);
    setSelectedCameraForSession(null);
    setTimeRange(null);
  };

  if (selectedSession && selectedCameraForSession !== null) {
    // Timeline view
    const cameraData = cameraRecordings.find(c => c.camera_id === selectedCameraForSession);
    const completeSegments = selectedSession.segments.filter(s => s.is_complete);
    
    return (
      <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4">
        <div className="bg-[var(--bg-2)] border border-[var(--border)] max-w-4xl w-full max-h-[90vh] flex flex-col">
          {/* Header */}
          <div className="flex items-center justify-between p-4 border-b border-[var(--border)]">
            <div className="flex items-center gap-3">
              <button
                onClick={handleBackToList}
                className="text-[var(--text-dim)] hover:text-[var(--text)] text-xl"
              >
                ←
              </button>
              <div>
                <h3 className="text-lg font-semibold text-[var(--text)]">{t('shared.recordingTimeline')}</h3>
                <p className="text-sm text-[var(--text-dim)]">
                  {cameraData?.camera_name} • {fmt.date(selectedSession.start_time)}
                </p>
              </div>
            </div>
            <button
              onClick={onClose}
              className="text-[var(--text-dim)] hover:text-[var(--text)] text-xl"
            >
              ✕
            </button>
          </div>

          {/* Session Info */}
          <div className="p-4 bg-[var(--panel-2)] border-b border-[var(--border)]">
            <div className="grid grid-cols-4 gap-4 text-sm">
              <div>
                <div className="text-[var(--text-dim)]">{t('shared.duration')}</div>
                <div className="text-[var(--text)] font-medium">{selectedSession.complete_duration_formatted}</div>
              </div>
              <div>
                <div className="text-[var(--text-dim)]">{t('shared.size')}</div>
                <div className="text-[var(--text)] font-medium">{selectedSession.size_formatted}</div>
              </div>
              <div>
                <div className="text-[var(--text-dim)]">{t('shared.segments')}</div>
                <div className="text-[var(--text)] font-medium">
                  {selectedSession.complete_segment_count} complete
                  {selectedSession.incomplete_segment_count > 0 && (
                    <span className="text-[var(--warn)] text-xs ml-1">
                      (+{selectedSession.incomplete_segment_count} in progress)
                    </span>
                  )}
                </div>
              </div>
              <div>
                <div className="text-[var(--text-dim)]">{t('shared.timeRange')}</div>
                <div className="text-[var(--text)] font-medium text-xs">
                  {fmt.time(selectedSession.start_time)} - {fmt.time(selectedSession.end_time)}
                </div>
              </div>
            </div>
            {selectedSession.is_in_progress && (
              <div className="mt-3 p-2 bg-[color-mix(in_oklab,var(--warn)_14%,transparent)] border border-[color-mix(in_oklab,var(--warn)_45%,var(--border))] text-xs text-[var(--warn)]">
                🔄 This recording is still in progress. Only complete segments will be analyzed.
              </div>
            )}
          </div>

          {/* Timeline */}
          <div className="flex-1 overflow-auto p-6">
            <RecordingTimeline
              segments={completeSegments}
              onSelectionChange={(start, end) => setTimeRange({ start, end })}
              onAnalyze={handleAnalyze}
            />
          </div>

          <div className="p-4 border-t border-[var(--border)] text-sm text-[var(--text-dim)]">
            💡 Drag on the timeline to select a specific time range, or click "Analyze All" to process the entire recording.
          </div>
        </div>
      </div>
    );
  }

  // Camera/Date/Session list view
  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4">
      <div className="bg-[var(--bg-2)] border border-[var(--border)] max-w-5xl w-full max-h-[85vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-[var(--border)]">
          <h3 className="text-lg font-semibold text-[var(--text)]">{t('shared.selectRecording')}</h3>
          <button
            onClick={onClose}
            className="text-[var(--text-dim)] hover:text-[var(--text)] text-xl"
          >
            ✕
          </button>
        </div>

        {/* Filter */}
        <div className="p-4 border-b border-[var(--border)] bg-[var(--panel-2)]">
          <div className="flex gap-3 items-center">
            <label className="text-sm text-[var(--text)]">Filter by Camera:</label>
            <select
              className="px-3 py-2 bg-[var(--panel-2)] border border-[var(--border)] text-[var(--text)] text-sm focus:outline-none focus:border-[var(--accent)]"
              value={selectedCameraId || ''}
              onChange={(e) => {
                const cameraId = e.target.value ? Number(e.target.value) : null;
                setSelectedCameraId(cameraId);
                loadRecordingSessions(cameraId || undefined);
              }}
            >
              <option value="">{t('shared.allCameras')}</option>
              {cameras.map((cam) => (
                <option key={cam.id} value={cam.id}>
                  {cam.name}
                </option>
              ))}
            </select>
            <button
              onClick={() => loadRecordingSessions(selectedCameraId || undefined)}
              className="px-4 py-2 bg-[var(--panel-2)] hover:bg-[var(--panel)] border border-[var(--border)] text-[var(--text)] text-sm transition-colors"
              disabled={loading}
            >
              {loading ? '⏳ Loading...' : '🔄 Refresh'}
            </button>
          </div>
        </div>

        {/* Recording List */}
        <div className="flex-1 overflow-auto p-4">
          {loading ? (
            <div className="text-center py-12 text-[var(--text-dim)]">
              <div className="text-4xl mb-2">⏳</div>
              Loading recordings...
            </div>
          ) : cameraRecordings.length === 0 ? (
            <div className="text-center py-12 text-[var(--text-dim)]">
              <div className="text-4xl mb-2">📹</div>
              <div>{t('shared.noRecordings')}</div>
              <div className="text-sm mt-2">Record some videos first to use them for AI processing</div>
            </div>
          ) : (
            <div className="space-y-4">
              {cameraRecordings.map((camera) => (
                <div key={camera.camera_id} className="bg-[var(--panel-2)] overflow-hidden">
                  {/* Camera Header */}
                  <div className="p-4 bg-[var(--panel-2)] border-b border-[var(--border)]">
                    <div className="flex items-center justify-between">
                      <div>
                        <h4 className="text-[var(--text)] font-semibold">📹 {camera.camera_name}</h4>
                        {camera.camera_location && (
                          <p className="text-sm text-[var(--text-dim)]">{camera.camera_location}</p>
                        )}
                      </div>
                      <div className="text-sm text-[var(--text-dim)]">
                        {camera.dates.length} {camera.dates.length === 1 ? 'date' : 'dates'}
                      </div>
                    </div>
                  </div>

                  {/* Dates */}
                  <div className="divide-y divide-[var(--border)]">
                    {camera.dates.map((dateData) => {
                      const dateKey = `${camera.camera_id}-${dateData.date}`;
                      const isExpanded = expandedDates.has(dateKey);

                      return (
                        <div key={dateData.date}>
                          {/* Date Header */}
                          <button
                            onClick={() => toggleDate(camera.camera_id, dateData.date)}
                            className="w-full p-3 flex items-center justify-between hover:bg-[var(--panel-2)] transition-colors text-left"
                          >
                            <div className="flex items-center gap-3">
                              <span className="text-[var(--text-dim)]">{isExpanded ? '▼' : '▶'}</span>
                              <div>
                                <div className="text-[var(--text)] font-medium">
                                  📅 {fmt.date(dateData.date, {
                                    weekday: 'long',
                                    year: 'numeric',
                                    month: 'long',
                                    day: 'numeric'
                                  })}
                                </div>
                                <div className="text-sm text-[var(--text-dim)]">
                                  {dateData.session_count} {dateData.session_count === 1 ? 'session' : 'sessions'}
                                </div>
                              </div>
                            </div>
                          </button>

                          {/* Sessions */}
                          {isExpanded && (
                            <div className="bg-[var(--bg-2)] p-3 space-y-2">
                              {dateData.sessions.map((session) => {
                                // Check if session has at least one complete segment
                                const hasCompleteSegments = session.complete_segment_count > 0;
                                const isPartiallyComplete = session.is_in_progress && hasCompleteSegments;
                                
                                return (
                                  <button
                                    key={session.session_id}
                                    onClick={() => handleSessionClick(session, camera.camera_id)}
                                    className={`w-full p-3 bg-[var(--panel-2)] hover:bg-[var(--panel-2)] border rounded transition-colors text-left ${
                                      !hasCompleteSegments
                                        ? 'border-[color-mix(in_oklab,var(--warn)_45%,var(--border))] hover:border-[var(--warn)] opacity-60 cursor-not-allowed' 
                                        : isPartiallyComplete
                                        ? 'border-[color-mix(in_oklab,var(--warn)_45%,var(--border))] hover:border-[var(--warn)]'
                                        : 'border-[var(--border)] hover:border-[var(--accent)]'
                                    }`}
                                    disabled={!hasCompleteSegments}
                                  >
                                    <div className="flex items-center justify-between">
                                      <div className="flex-1">
                                        <div className="flex items-center gap-3 text-[var(--text)] font-medium mb-1">
                                          <span>{!hasCompleteSegments ? '⚠️' : isPartiallyComplete ? '🔄' : '🎬'}</span>
                                          <span>
                                            {fmt.time(session.start_time)} - {fmt.time(session.end_time)}
                                          </span>
                                          {!hasCompleteSegments && (
                                            <span className="text-xs px-2 py-1 bg-[color-mix(in_oklab,var(--warn)_14%,transparent)] text-[var(--warn)]">
                                              No Complete Segments
                                            </span>
                                          )}
                                          {isPartiallyComplete && (
                                            <span className="text-xs px-2 py-1 bg-[color-mix(in_oklab,var(--warn)_14%,transparent)] text-[var(--warn)]">
                                              Recording in Progress
                                            </span>
                                          )}
                                        </div>
                                        <div className="flex gap-4 text-sm text-[var(--text-dim)]">
                                          {hasCompleteSegments ? (
                                            <>
                                              <span>Duration: {session.complete_duration_formatted}</span>
                                              <span>Size: {session.size_formatted}</span>
                                              <span>
                                                {session.complete_segment_count} complete
                                                {session.incomplete_segment_count > 0 && ` + ${session.incomplete_segment_count} in progress`}
                                              </span>
                                            </>
                                          ) : (
                                            <>
                                              <span>Duration: N/A</span>
                                              <span>Size: N/A</span>
                                              <span>{session.segment_count} {session.segment_count === 1 ? 'segment' : 'segments'}</span>
                                            </>
                                          )}
                                        </div>
                                      </div>
                                      {hasCompleteSegments && (
                                        <div className="px-4 py-2 bg-[var(--accent)] text-white text-sm font-medium">
                                          Select →
                                        </div>
                                      )}
                                    </div>
                                  </button>
                                );
                              })}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="p-4 border-t border-[var(--border)] text-sm text-[var(--text-dim)] bg-[var(--panel-2)]">
          💡 Browse by camera and date, then select a recording session to configure AI analysis with timeline selection.
        </div>
      </div>
    </div>
  );
};
