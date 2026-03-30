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

import React, { useState, useRef, useEffect } from 'react';

interface TimelineSegment {
  path: string;
  start_time: string;
  end_time: string;
  duration_seconds: number;
}

interface RecordingTimelineProps {
  segments: TimelineSegment[];
  onSelectionChange: (startTime: string, endTime: string) => void;
  onAnalyze: () => void;
}

export const RecordingTimeline: React.FC<RecordingTimelineProps> = ({
  segments,
  onSelectionChange,
  onAnalyze
}) => {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [selectionStart, setSelectionStart] = useState<number | null>(null);
  const [selectionEnd, setSelectionEnd] = useState<number | null>(null);
  const [selectedRange, setSelectedRange] = useState<{start: string, end: string} | null>(null);

  // Calculate timeline bounds
  const timelineStart = segments.length > 0 ? new Date(segments[0].start_time).getTime() : 0;
  const timelineEnd = segments.length > 0 ? new Date(segments[segments.length - 1].end_time).getTime() : 0;
  const totalDuration = timelineEnd - timelineStart;

  useEffect(() => {
    drawTimeline();
  }, [segments, selectionStart, selectionEnd]);

  const drawTimeline = () => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const width = canvas.width;
    const height = canvas.height;

    // Clear canvas
    ctx.clearRect(0, 0, width, height);

    // Draw background
    ctx.fillStyle = '#1e293b';
    ctx.fillRect(0, 0, width, height);

    // Draw segments
    segments.forEach((segment) => {
      const segStart = new Date(segment.start_time).getTime();
      const segEnd = new Date(segment.end_time).getTime();
      
      const x = ((segStart - timelineStart) / totalDuration) * width;
      const w = ((segEnd - segStart) / totalDuration) * width;

      // Segment bar
      ctx.fillStyle = '#3b82f6';
      ctx.fillRect(x, 20, w, height - 40);

      // Segment border
      ctx.strokeStyle = '#1e40af';
      ctx.lineWidth = 1;
      ctx.strokeRect(x, 20, w, height - 40);
    });

    // Draw selection
    if (selectionStart !== null && selectionEnd !== null) {
      const start = Math.min(selectionStart, selectionEnd);
      const end = Math.max(selectionStart, selectionEnd);

      ctx.fillStyle = 'rgba(34, 197, 94, 0.3)';
      ctx.fillRect(start, 0, end - start, height);

      ctx.strokeStyle = '#22c55e';
      ctx.lineWidth = 2;
      ctx.strokeRect(start, 0, end - start, height);
    }

    // Draw time labels
    ctx.fillStyle = '#94a3b8';
    ctx.font = '12px sans-serif';
    ctx.textAlign = 'left';
    
    const startDate = new Date(timelineStart);
    const endDate = new Date(timelineEnd);
    
    ctx.fillText(formatTime(startDate), 5, 15);
    ctx.textAlign = 'right';
    ctx.fillText(formatTime(endDate), width - 5, 15);
  };

  const formatTime = (date: Date) => {
    return date.toLocaleTimeString('en-US', { 
      hour: '2-digit', 
      minute: '2-digit',
      second: '2-digit'
    });
  };

  const formatDuration = (ms: number) => {
    const totalSeconds = Math.floor(ms / 1000);
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;

    if (hours > 0) {
      return `${hours}h ${minutes}m ${seconds}s`;
    } else if (minutes > 0) {
      return `${minutes}m ${seconds}s`;
    } else {
      return `${seconds}s`;
    }
  };

  const handleMouseDown = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const rect = canvas.getBoundingClientRect();
    const x = e.clientX - rect.left;

    setIsDragging(true);
    setSelectionStart(x);
    setSelectionEnd(x);
  };

  const handleMouseMove = (e: React.MouseEvent<HTMLCanvasElement>) => {
    if (!isDragging) return;

    const canvas = canvasRef.current;
    if (!canvas) return;

    const rect = canvas.getBoundingClientRect();
    const x = e.clientX - rect.left;

    setSelectionEnd(x);
  };

  const handleMouseUp = () => {
    if (!isDragging || selectionStart === null || selectionEnd === null) return;

    const canvas = canvasRef.current;
    if (!canvas) return;

    const width = canvas.width;
    
    const start = Math.min(selectionStart, selectionEnd);
    const end = Math.max(selectionStart, selectionEnd);

    // Convert pixel positions to timestamps
    const startTime = new Date(timelineStart + (start / width) * totalDuration);
    const endTime = new Date(timelineStart + (end / width) * totalDuration);

    setSelectedRange({
      start: startTime.toISOString(),
      end: endTime.toISOString()
    });

    onSelectionChange(startTime.toISOString(), endTime.toISOString());
    setIsDragging(false);
  };

  const handleClearSelection = () => {
    setSelectionStart(null);
    setSelectionEnd(null);
    setSelectedRange(null);
  };

  return (
    <div className="bg-slate-800 p-4 rounded-lg">
      <div className="mb-3">
        <h4 className="text-sm font-medium text-white mb-1">Recording Timeline</h4>
        <p className="text-xs text-slate-400">
          Drag on the timeline to select a time range to analyze
        </p>
      </div>

      <canvas
        ref={canvasRef}
        width={800}
        height={80}
        className="w-full border border-slate-700 rounded cursor-crosshair"
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onMouseLeave={() => setIsDragging(false)}
      />

      {selectedRange && (
        <div className="mt-3 p-3 bg-slate-700 rounded text-sm space-y-1">
          <div className="flex justify-between items-center">
            <span className="text-slate-300">Selected Range:</span>
            <button
              onClick={handleClearSelection}
              className="text-xs text-slate-400 hover:text-white"
            >
              Clear
            </button>
          </div>
          <div className="text-white font-mono text-xs">
            {formatTime(new Date(selectedRange.start))} - {formatTime(new Date(selectedRange.end))}
          </div>
          <div className="text-slate-400 text-xs">
            Duration: {formatDuration(new Date(selectedRange.end).getTime() - new Date(selectedRange.start).getTime())}
          </div>
        </div>
      )}

      <div className="mt-3 flex gap-2">
        <button
          onClick={onAnalyze}
          disabled={!selectedRange}
          className="flex-1 px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:bg-slate-700 disabled:text-slate-500 text-white rounded font-medium transition-colors"
        >
          {selectedRange ? 'Analyze Selected Range' : 'Select a range first'}
        </button>
        <button
          onClick={() => {
            handleClearSelection();
            onAnalyze();
          }}
          className="px-4 py-2 bg-slate-700 hover:bg-slate-600 text-white rounded font-medium transition-colors"
        >
          Analyze All
        </button>
      </div>
    </div>
  );
};
