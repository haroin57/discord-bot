export type Mood = 'good' | 'normal' | 'bad' | (string & {})
export type EventType =
  | 'routine'
  | 'minor_event'
  | 'major_event'
  | 'breakdown'
  | (string & {})

export interface KanaeEntry {
  ts: string
  event_type: EventType
  importance: number
  activity: string
  detail: string
  mood: Mood
  thought?: string
}

export interface KanaeDaily {
  entries: KanaeEntry[]
  [key: string]: unknown
}
