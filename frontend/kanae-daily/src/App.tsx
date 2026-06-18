import { useEffect, useMemo, useRef, useState } from 'react'
import type { KanaeDaily, KanaeEntry } from './types'
import './App.css'

const POLL_INTERVAL_MS = 5_000

type FetchState =
  | { kind: 'loading' }
  | { kind: 'ok'; data: KanaeDaily; mtime: string | null; fetchedAt: Date }
  | { kind: 'error'; message: string }

const MOOD_LABEL: Record<string, string> = {
  good: 'good',
  normal: 'normal',
  bad: 'bad',
}

const EVENT_LABEL: Record<string, string> = {
  routine: 'routine',
  minor_event: 'minor',
  major_event: 'major',
  breakdown: 'breakdown',
}

function formatTime(ts: string): { day: string; time: string } {
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return { day: ts, time: '' }
  const pad = (n: number) => String(n).padStart(2, '0')
  const day = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}`
  return { day, time }
}

function importanceStars(n: number): string {
  const clamped = Math.max(0, Math.min(10, Math.round(n)))
  return '★'.repeat(clamped) + '☆'.repeat(Math.max(0, 5 - clamped))
}

async function fetchDaily(signal: AbortSignal): Promise<{
  data: KanaeDaily
  mtime: string | null
}> {
  const res = await fetch('/api/kanae-daily', { signal, cache: 'no-store' })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`HTTP ${res.status}: ${text.slice(0, 200)}`)
  }
  const data = (await res.json()) as KanaeDaily
  return { data, mtime: res.headers.get('X-Source-Mtime') }
}

function App() {
  const [state, setState] = useState<FetchState>({ kind: 'loading' })
  const [moodFilter, setMoodFilter] = useState<string>('all')
  const [eventFilter, setEventFilter] = useState<string>('all')
  const [minImportance, setMinImportance] = useState<number>(0)
  const [order, setOrder] = useState<'desc' | 'asc'>('desc')
  const [paused, setPaused] = useState(false)
  const prevTopTsRef = useRef<string | null>(null)
  const [flashTs, setFlashTs] = useState<string | null>(null)

  useEffect(() => {
    if (paused) return
    const ctl = new AbortController()
    let timer: number | undefined
    let stopped = false

    const tick = async () => {
      try {
        const { data, mtime } = await fetchDaily(ctl.signal)
        if (stopped) return
        setState({
          kind: 'ok',
          data,
          mtime,
          fetchedAt: new Date(),
        })
      } catch (e) {
        if (stopped) return
        if ((e as Error).name === 'AbortError') return
        setState({ kind: 'error', message: (e as Error).message })
      } finally {
        if (!stopped) {
          timer = window.setTimeout(tick, POLL_INTERVAL_MS)
        }
      }
    }

    tick()

    return () => {
      stopped = true
      ctl.abort()
      if (timer) window.clearTimeout(timer)
    }
  }, [paused])

  const entries: KanaeEntry[] = state.kind === 'ok' ? state.data.entries ?? [] : []

  const sorted = useMemo(() => {
    const copy = [...entries]
    copy.sort((a, b) => {
      const ta = new Date(a.ts).getTime()
      const tb = new Date(b.ts).getTime()
      return order === 'desc' ? tb - ta : ta - tb
    })
    return copy
  }, [entries, order])

  const filtered = useMemo(() => {
    return sorted.filter((e) => {
      if (moodFilter !== 'all' && e.mood !== moodFilter) return false
      if (eventFilter !== 'all' && e.event_type !== eventFilter) return false
      if ((e.importance ?? 0) < minImportance) return false
      return true
    })
  }, [sorted, moodFilter, eventFilter, minImportance])

  const moodOptions = useMemo(() => {
    return Array.from(new Set(entries.map((e) => e.mood))).filter(Boolean)
  }, [entries])
  const eventOptions = useMemo(() => {
    return Array.from(new Set(entries.map((e) => e.event_type))).filter(Boolean)
  }, [entries])

  const stats = useMemo(() => {
    const total = entries.length
    const moodCount: Record<string, number> = {}
    const eventCount: Record<string, number> = {}
    let impSum = 0
    for (const e of entries) {
      moodCount[e.mood] = (moodCount[e.mood] ?? 0) + 1
      eventCount[e.event_type] = (eventCount[e.event_type] ?? 0) + 1
      impSum += e.importance ?? 0
    }
    return {
      total,
      moodCount,
      eventCount,
      avgImportance: total ? impSum / total : 0,
    }
  }, [entries])

  useEffect(() => {
    if (state.kind !== 'ok') return
    const top = sorted[0]?.ts ?? null
    if (top && prevTopTsRef.current && top !== prevTopTsRef.current) {
      setFlashTs(top)
      const id = window.setTimeout(() => setFlashTs(null), 2_500)
      return () => window.clearTimeout(id)
    }
    prevTopTsRef.current = top
  }, [sorted, state])

  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar__title">
          <h1>かなえ daily</h1>
          <span className="topbar__path">.state/kanae_daily.json</span>
        </div>
        <div className="topbar__status">
          {state.kind === 'loading' && <span className="dot dot--loading" />}
          {state.kind === 'ok' && <span className="dot dot--ok" />}
          {state.kind === 'error' && <span className="dot dot--err" />}
          <span className="topbar__statusText">
            {state.kind === 'loading' && 'loading'}
            {state.kind === 'ok' &&
              `${entries.length} entries · 更新 ${state.fetchedAt.toLocaleTimeString()}`}
            {state.kind === 'error' && state.message}
          </span>
          <button
            type="button"
            className={`pauseBtn ${paused ? 'pauseBtn--paused' : ''}`}
            onClick={() => setPaused((p) => !p)}
          >
            {paused ? '再開' : '一時停止'}
          </button>
        </div>
      </header>

      <section className="stats">
        <div className="stat">
          <div className="stat__label">total</div>
          <div className="stat__value">{stats.total}</div>
        </div>
        <div className="stat">
          <div className="stat__label">avg importance</div>
          <div className="stat__value">{stats.avgImportance.toFixed(2)}</div>
        </div>
        <div className="stat stat--wide">
          <div className="stat__label">mood</div>
          <div className="stat__bars">
            {Object.entries(stats.moodCount).map(([m, c]) => (
              <span key={m} className={`pill pill--mood-${m}`}>
                {MOOD_LABEL[m] ?? m} {c}
              </span>
            ))}
          </div>
        </div>
        <div className="stat stat--wide">
          <div className="stat__label">event</div>
          <div className="stat__bars">
            {Object.entries(stats.eventCount).map(([m, c]) => (
              <span key={m} className={`pill pill--event-${m}`}>
                {EVENT_LABEL[m] ?? m} {c}
              </span>
            ))}
          </div>
        </div>
      </section>

      <section className="filters">
        <label className="filter">
          <span>mood</span>
          <select
            value={moodFilter}
            onChange={(e) => setMoodFilter(e.target.value)}
          >
            <option value="all">all</option>
            {moodOptions.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </label>
        <label className="filter">
          <span>event</span>
          <select
            value={eventFilter}
            onChange={(e) => setEventFilter(e.target.value)}
          >
            <option value="all">all</option>
            {eventOptions.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </label>
        <label className="filter">
          <span>importance ≥ {minImportance}</span>
          <input
            type="range"
            min={0}
            max={10}
            value={minImportance}
            onChange={(e) => setMinImportance(Number(e.target.value))}
          />
        </label>
        <label className="filter">
          <span>order</span>
          <select
            value={order}
            onChange={(e) => setOrder(e.target.value as 'asc' | 'desc')}
          >
            <option value="desc">新しい順</option>
            <option value="asc">古い順</option>
          </select>
        </label>
        <div className="filter filter--meta">
          表示 {filtered.length} / {entries.length}
        </div>
      </section>

      <main className="timeline">
        {filtered.map((entry) => {
          const { day, time } = formatTime(entry.ts)
          const isFlash = flashTs === entry.ts
          return (
            <article
              key={entry.ts}
              className={[
                'card',
                `card--mood-${entry.mood}`,
                `card--event-${entry.event_type}`,
                isFlash ? 'card--flash' : '',
              ]
                .filter(Boolean)
                .join(' ')}
            >
              <header className="card__head">
                <time className="card__time" dateTime={entry.ts}>
                  <span className="card__day">{day}</span>
                  <span className="card__hm">{time}</span>
                </time>
                <span className={`pill pill--event-${entry.event_type}`}>
                  {EVENT_LABEL[entry.event_type] ?? entry.event_type}
                </span>
                <span className={`pill pill--mood-${entry.mood}`}>
                  {MOOD_LABEL[entry.mood] ?? entry.mood}
                </span>
                <span
                  className="pill pill--importance"
                  title={`importance ${entry.importance}`}
                >
                  {importanceStars(entry.importance)}
                </span>
              </header>
              <h2 className="card__activity">{entry.activity}</h2>
              <p className="card__detail">{entry.detail}</p>
              {entry.thought && (
                <blockquote className="card__thought">
                  {entry.thought}
                </blockquote>
              )}
            </article>
          )
        })}
        {filtered.length === 0 && state.kind === 'ok' && (
          <div className="empty">該当するエントリがありません</div>
        )}
      </main>
    </div>
  )
}

export default App
