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

import React, { useEffect, useMemo, useState } from "react";
import { useNavigate } from 'react-router-dom'
import { Card, CardHeader, CardTitle, CardContent, Skeleton, ErrorCard } from "./Dashboard";
import { ResponsiveContainer, AreaChart, Area, XAxis, YAxis, Tooltip as RTooltip, CartesianGrid, BarChart, Bar, Cell } from 'recharts'
import { apiService } from "../lib/apiService";

export default function SystemNetworkMonitoring() {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [stats, setStats] = useState<any | null>(null);
  const navigate = useNavigate()

  useEffect(() => {
    setLoading(true);
    setError(null);
    apiService
      .getSuricataStats({ limit: 5000 })
      .then(({ data }) => {
        setStats(data);
        setLoading(false);
      })
      .catch(() => {
        setError("Failed to fetch Suricata stats");
        setLoading(false);
      });
  }, []);

  const severities = useMemo(() => {
    const map = stats?.by_severity || {};
    const labels: Record<string, string> = { "1": "High", "2": "Medium", "3": "Low", unknown: "Unknown" };
    return Object.entries(map).map(([k, v]) => ({ name: labels[k] || k, value: v as number }));
  }, [stats]);

  const timeseries = useMemo(() => {
    return (stats?.timeseries || []).map((d: any) => ({ ts: d.ts, count: d.count }));
  }, [stats]);

  const topCategoryName = useMemo(() => {
    return stats?.by_category?.[0]?.name || null
  }, [stats])

  return (
    <section className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>System &amp; Network Monitoring</CardTitle>
        </CardHeader>
        <CardContent>
          {loading ? (
            <Skeleton className="h-24" />
          ) : error ? (
            <ErrorCard title="Suricata" message={error} />
          ) : (
            <>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              <button
                className="text-left rounded border border-neutral-700 p-3 bg-[var(--panel-2)] hover:bg-[var(--panel)] transition-colors"
                onClick={() => navigate('/alerts-incidents?only_alerts=1')}
                title="View all alerts"
              >
                <div className="text-xs text-[var(--text-dim)]">Total Alerts</div>
                <div className="text-xl font-semibold">{stats?.total_alerts ?? 0}</div>
              </button>
              <button
                className="text-left rounded border border-neutral-700 p-3 bg-[var(--panel-2)] hover:bg-[var(--panel)] transition-colors"
                onClick={() => navigate('/alerts-incidents?only_alerts=1&severity=1')}
                title="View high severity alerts"
              >
                <div className="text-xs text-[var(--text-dim)]">High Severity</div>
                <div className="text-xl font-semibold">{severities.find(s => s.name === "High")?.value ?? 0}</div>
              </button>
              <button
                className={`text-left rounded border border-neutral-700 p-3 bg-[var(--panel-2)] ${topCategoryName ? 'hover:bg-[var(--panel)] cursor-pointer' : 'opacity-70 cursor-not-allowed'} transition-colors`}
                onClick={() => topCategoryName && navigate(`/alerts-incidents?only_alerts=1&category=${encodeURIComponent(topCategoryName)}`)}
                title={topCategoryName ? `View ${topCategoryName} alerts` : 'No category data'}
                disabled={!topCategoryName}
              >
                <div className="text-xs text-[var(--text-dim)]">Top Category</div>
                <div className="text-sm">{topCategoryName || "—"}</div>
              </button>
            </div>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 mt-3">
              <Card>
                <CardHeader>
                  <CardTitle>Alerts over time</CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="h-56">
                    <ResponsiveContainer width="100%" height="100%">
                      <AreaChart data={timeseries} margin={{ top: 10, right: 20, left: 0, bottom: 0 }}>
                        <defs>
                          <linearGradient id="suricataAlertGrad" x1="0" y1="0" x2="0" y2="1">
                            <stop offset="5%" stopColor="#fca5a5" stopOpacity={0.5} />
                            <stop offset="95%" stopColor="#fca5a5" stopOpacity={0} />
                          </linearGradient>
                        </defs>
                        <CartesianGrid strokeDasharray="3 3" stroke="rgba(148,163,184,0.15)" />
                        <XAxis dataKey="ts" stroke="var(--text-dim)" fontSize={12} tickFormatter={(v) => new Date(v).toLocaleString()} />
                        <YAxis stroke="var(--text-dim)" fontSize={12} allowDecimals={false} />
                        <RTooltip contentStyle={{ background: 'var(--panel-2)', border: '1px solid rgb(64,64,64)', color: 'var(--text)' }} labelFormatter={(v) => new Date(v as string).toLocaleString()} />
                        <Area type="monotone" dataKey="count" stroke="#ef4444" fill="url(#suricataAlertGrad)" strokeWidth={2} />
                      </AreaChart>
                    </ResponsiveContainer>
                  </div>
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle>Severity distribution</CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="h-56">
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart data={severities} margin={{ top: 10, right: 20, left: 0, bottom: 0 }}>
                        <CartesianGrid strokeDasharray="3 3" stroke="rgba(148,163,184,0.15)" />
                        <XAxis dataKey="name" stroke="var(--text-dim)" fontSize={12} />
                        <YAxis stroke="var(--text-dim)" fontSize={12} allowDecimals={false} />
                        <RTooltip contentStyle={{ background: 'var(--panel-2)', border: '1px solid rgb(64,64,64)', color: 'var(--text)' }} />
                        <Bar dataKey="value" radius={[4, 4, 0, 0]}>
                          {severities.map((entry: any) => {
                            const color = entry.name === 'High' ? '#ef4444' : entry.name === 'Medium' ? '#34d399' : entry.name === 'Low' ? '#60a5fa' : '#94a3b8'
                            return <Cell key={entry.name} fill={color} />
                          })}
                        </Bar>
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                </CardContent>
              </Card>
            </div>
            </>
          )}
        </CardContent>
      </Card>
    </section>
  );
}
