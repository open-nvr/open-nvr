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


import React, { useEffect, useRef, useState } from 'react';
import { usePushNotification } from '../components/usePushNotification';

// Robust parser for Suricata fast.log lines
function parseFastLog(line: string) {
  // Try to match the full format
  // Allow multiple spaces between fields
  const regex = /^(\S+)\s+\[\*\*\]\s+\[(.*?)\]\s+(.*?)\s+\[\*\*\]\s+\[Classification: (.*?)\]\s+\[Priority: (\d+)\]\s+\{(\w+)\}\s+(\S+):(\d+) -> (\S+):(\d+)/;
  const m = line.match(regex);
  if (m) {
    return {
      time: m[1],
      sig: m[2],
      message: m[3],
      classification: m[4],
      priority: m[5],
      proto: m[6],
      src: m[7],
      srcPort: m[8],
      dst: m[9],
      dstPort: m[10],
    };
  }
  // Fallback: try to extract time and message
  const fallback = /^(\S+) .*?\[\*\*\] (.*?) \[\*\*\]/.exec(line);
  return {
    time: fallback ? fallback[1] : '',
    sig: '',
    message: fallback ? fallback[2] : line,
    classification: '',
    priority: '',
    proto: '',
    src: '',
    srcPort: '',
    dst: '',
    dstPort: '',
    raw: line,
  };
}

export function SuricataAlertStream() {
  const [lines, setLines] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const eventSourceRef = useRef<EventSource | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    let isMounted = true;
    let reader: ReadableStreamDefaultReader | null = null;
    let abortController = new AbortController();
    abortRef.current = abortController;
    setError(null);

    async function fetchStream() {
      try {
        const resp = await fetch('/api/v1/suricata/alerts/stream', { signal: abortController.signal });
        if (!resp.body) throw new Error('No response body');
        reader = resp.body.getReader();
        let buffer = '';
        while (isMounted) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += new TextDecoder().decode(value);
          let parts = buffer.split('\n');
          buffer = parts.pop() || '';
          if (parts.length > 0) {
            setLines(prev => [...prev, ...parts].slice(-200));
          }
        }
      } catch (e: any) {
        if (!abortController.signal.aborted) setError(e.message || 'Stream error');
      }
    }
    fetchStream();
    return () => {
      isMounted = false;
      abortController.abort();
      if (reader) reader.cancel();
    };
  }, []);

  // Parse lines into alert objects
  const alerts = lines
    .map(parseFastLog)
    .filter(Boolean) as ReturnType<typeof parseFastLog>[];

  // Mark as read state: index of last alert notified
  const [lastReadIdx, setLastReadIdx] = useState<number>(() => {
    // Persist in localStorage for session continuity
    const v = localStorage.getItem('suricata.lastReadIdx');
    return v ? parseInt(v, 10) : alerts.length - 1;
  });
  // Update localStorage when lastReadIdx changes
  useEffect(() => { localStorage.setItem('suricata.lastReadIdx', String(lastReadIdx)); }, [lastReadIdx]);

  // Notify for new alerts only

  useEffect(() => {
    if (alerts.length > 0 && lastReadIdx < alerts.length - 1) {
      for (let i = lastReadIdx + 1; i < alerts.length; ++i) {
        const a = alerts[i];
        if (document.visibilityState !== 'visible') {
          if (window.Notification && Notification.permission === 'granted') {
            new Notification(
              `Suricata Alert: ${a.message || 'New alert'}`,
              {
                body: `${a.time || ''} ${a.classification ? '[' + a.classification + ']' : ''} ${a.src ? a.src + (a.srcPort ? ':' + a.srcPort : '') : ''} -> ${a.dst ? a.dst + (a.dstPort ? ':' + a.dstPort : '') : ''}`
              }
            );
          } else if (window.Notification && Notification.permission !== 'denied') {
            Notification.requestPermission().then(permission => {
              if (permission === 'granted') {
                new Notification(
                  `Suricata Alert: ${a.message || 'New alert'}`,
                  {
                    body: `${a.time || ''} ${a.classification ? '[' + a.classification + ']' : ''} ${a.src ? a.src + (a.srcPort ? ':' + a.srcPort : '') : ''} -> ${a.dst ? a.dst + (a.dstPort ? ':' + a.dstPort : '') : ''}`
                  }
                );
              }
            });
          }
        }
      }
      setLastReadIdx(alerts.length - 1);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [alerts.length]);

  // Mark all as read handler
  const markAllAsRead = () => setLastReadIdx(alerts.length - 1);

  return (
    <div className="mb-4">
      <div className="flex items-center gap-2 mb-1">
        <h2 className="text-base font-semibold">Detected Anomalies</h2>
        <button className="btn btn-primary text-xs py-1 px-2" style={{marginLeft: 8}} onClick={markAllAsRead} disabled={alerts.length === 0 || lastReadIdx === alerts.length - 1}>
          Mark all as read
        </button>
        {error && <span className="text-red-400 text-xs">{error}</span>}
      </div>
      <div className="overflow-auto border border-neutral-700 bg-[var(--panel-2)] rounded" style={{ maxHeight: 240, minHeight: 80 }}>
        <table className="w-full text-xs font-mono table-fixed">
          <thead className="bg-[var(--panel-2)] text-left sticky top-0">
            <tr>
              <th className="p-2 w-[140px]">Time</th>
              <th className="p-2 w-[220px]">Message</th>
              <th className="p-2 w-[120px]">Class</th>
              <th className="p-2 w-[80px]">Priority</th>
              <th className="p-2 w-[80px]">Proto</th>
              <th className="p-2 w-[160px]">Source</th>
              <th className="p-2 w-[160px]">Dest</th>
            </tr>
          </thead>
          <tbody>
            {alerts.length === 0 && !error && (
              <tr><td colSpan={7} className="text-center text-[var(--text-dim)] py-2">Waiting for alerts…</td></tr>
            )}
            {alerts.map((a, i) => (
              <tr key={i} className={i % 2 === 0 ? "bg-[var(--bg-2)]" : "bg-[var(--panel)]"}>
                <td className="p-2 whitespace-nowrap">{a.time || '-'}</td>
                <td className="p-2 truncate" title={a.message || a.raw || '-'}>{a.message || a.raw || '-'}</td>
                <td className="p-2">{a.classification || '-'}</td>
                <td className="p-2">{a.priority || '-'}</td>
                <td className="p-2">{a.proto || '-'}</td>
                <td className="p-2">{a.src}{a.srcPort ? `:${a.srcPort}` : ''}</td>
                <td className="p-2">{a.dst}{a.dstPort ? `:${a.dstPort}` : ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
