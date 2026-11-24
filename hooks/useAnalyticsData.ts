/**
 * useAnalyticsData — React hook for fetching analytics from pre-computed API
 * 
 * The frontend is a FACT EXPLORER, not a calculation engine.
 * All metrics come from the backend, pre-aggregated in daily_analytics.
 */

import { useState, useEffect, useCallback } from "react";
import type {
  AnalyticsOverview,
  DailyAnalyticsRow,
  WeeklyAnalyticsRow,
  DecisionBreakdown,
  ExecutionQuality,
  RiskEventRecord,
  SystemStateSnapshot,
  ExecutionBlockedData,
  OpsEventRecord,
} from "./useAnalytics";

const API_BASE = "/api/analytics";

function formatDate(d: Date): string {
  return d.toISOString().split("T")[0];
}

function defaultFrom(): string {
  const d = new Date();
  d.setDate(d.getDate() - 30);
  return formatDate(d);
}

function defaultTo(): string {
  return formatDate(new Date());
}

async function fetchJSON<T>(url: string): Promise<T | null> {
  try {
    const res = await fetch(url);
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

export function useAnalyticsOverview(from?: string, to?: string) {
  const [data, setData] = useState<AnalyticsOverview | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    const f = from || defaultFrom();
    const t = to || defaultTo();
    const result = await fetchJSON<AnalyticsOverview>(
      `${API_BASE}/overview?from=${f}&to=${t}`
    );
    setData(result);
    setLoading(false);
  }, [from, to]);

  useEffect(() => { load(); }, [load]);
  return { data, loading, refresh: load };
}

export function useDailySeries(from?: string, to?: string) {
  const [data, setData] = useState<DailyAnalyticsRow[]>([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    const f = from || defaultFrom();
    const t = to || defaultTo();
    const result = await fetchJSON<DailyAnalyticsRow[]>(
      `${API_BASE}/daily-series?from=${f}&to=${t}`
    );
    setData(result || []);
    setLoading(false);
  }, [from, to]);

  useEffect(() => { load(); }, [load]);
  return { data, loading, refresh: load };
}

export function useWeeklySeries(from?: string, to?: string) {
  const [data, setData] = useState<WeeklyAnalyticsRow[]>([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    const f = from || defaultFrom();
    const t = to || defaultTo();
    const result = await fetchJSON<WeeklyAnalyticsRow[]>(
      `${API_BASE}/weekly-series?from=${f}&to=${t}`
    );
    setData(result || []);
    setLoading(false);
  }, [from, to]);

  useEffect(() => { load(); }, [load]);
  return { data, loading, refresh: load };
}

export function useDecisionBreakdown(from?: string, to?: string) {
  const [data, setData] = useState<DecisionBreakdown | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    const f = from || defaultFrom();
    const t = to || defaultTo();
    const result = await fetchJSON<DecisionBreakdown>(
      `${API_BASE}/decision-breakdown?from=${f}&to=${t}`
    );
    setData(result);
    setLoading(false);
  }, [from, to]);

  useEffect(() => { load(); }, [load]);
  return { data, loading, refresh: load };
}

export function useExecutionQuality(from?: string, to?: string) {
  const [data, setData] = useState<ExecutionQuality | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    const f = from || defaultFrom();
    const t = to || defaultTo();
    const result = await fetchJSON<ExecutionQuality>(
      `${API_BASE}/execution-quality?from=${f}&to=${t}`
    );
    setData(result);
    setLoading(false);
  }, [from, to]);

  useEffect(() => { load(); }, [load]);
  return { data, loading, refresh: load };
}

export function useRiskEvents(from?: string, to?: string, limit = 100) {
  const [data, setData] = useState<RiskEventRecord[]>([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    const f = from || defaultFrom();
    const t = to || defaultTo();
    const result = await fetchJSON<RiskEventRecord[]>(
      `${API_BASE}/risk-events?from=${f}&to=${t}&limit=${limit}`
    );
    setData(result || []);
    setLoading(false);
  }, [from, to, limit]);

  useEffect(() => { load(); }, [load]);
  return { data, loading, refresh: load };
}

export function useRegimeHistory(from?: string, to?: string, limit = 500) {
  const [data, setData] = useState<SystemStateSnapshot[]>([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    const f = from || defaultFrom();
    const t = to || defaultTo();
    const result = await fetchJSON<SystemStateSnapshot[]>(
      `${API_BASE}/regime-history?from=${f}&to=${t}&limit=${limit}`
    );
    setData(result || []);
    setLoading(false);
  }, [from, to, limit]);

  useEffect(() => { load(); }, [load]);
  return { data, loading, refresh: load };
}

export function useExecutionBlocked(from?: string, to?: string, limit = 500) {
  const [data, setData] = useState<ExecutionBlockedData | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    const f = from || defaultFrom();
    const t = to || defaultTo();
    const result = await fetchJSON<ExecutionBlockedData>(
      `${API_BASE}/execution-blocked?from=${f}&to=${t}&limit=${limit}`
    );
    setData(result);
    setLoading(false);
  }, [from, to, limit]);

  useEffect(() => { load(); }, [load]);
  return { data, loading, refresh: load };
}

export function useOpsEvents(from?: string, to?: string, limit = 100) {
  const [data, setData] = useState<OpsEventRecord[]>([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    const f = from || defaultFrom();
    const t = to || defaultTo();
    const result = await fetchJSON<OpsEventRecord[]>(
      `${API_BASE}/ops-events?from=${f}&to=${t}&limit=${limit}`
    );
    setData(result || []);
    setLoading(false);
  }, [from, to, limit]);

  useEffect(() => { load(); }, [load]);
  return { data, loading, refresh: load };
}
