/**
 * Bot Activity — live per-Bot activity feed (turns, tool calls, approvals, Group Chat streams).
 *
 * Desktop half of the `bot-activity` package. Rows come from the Python half two ways:
 * pushed live over `plugin.bot-activity.changed`, and pulled from `/activity?since=` as the
 * backlog + polling fallback (the push only reaches us from the Desktop backend process).
 *
 * Plain ESM, loaded uncompiled — UI is jsx() calls, not JSX syntax.
 */

import {
  atom, Badge, Button, Codicon, CopyButton, EmptyState, host, profileColor, relativeTime,
  queryClient, ROUTES_AREA, ScrollArea, SegmentedControl, SIDEBAR_NAV_AREA, StatusDot, Tip, cn, usePluginI18n,
  useQuery, useValue
} from '@hermes/plugin-sdk'
import { useEffect, useId, useMemo, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'bot-activity'
const EVENT = `plugin.${ID}.changed`
const ROUTE = '/bot-activity'
const MAX_ROWS = 1500
const MAX_GROUPS = 80
const POLL_MS = 4000
const ROSTER_POLL_MS = 5000
const STALE_MS = 15 * 60 * 1000
// Room streams never get an end frame; quiet this long = finished (mirrors STREAM_QUIET_S).
const STREAM_QUIET_MS = 30 * 1000
const ROOMS_POLL_MS = 10000
const SETUP_POLL_MS = 15000
// One Bot per request: `hermes plugins install` (server budget 240 s) or copy + enable (150 s) + slack.
const TRACK_TIMEOUT_MS = 260000
const ACTIVE = new Set(['running', 'streaming', 'waiting'])
const ALL = '__all__'
// Section key for activity outside any Group Chat (1:1 Bot chats, cron, bot-to-bot).
const DIRECT = '__direct__'
const VIEW_STORAGE_KEY = 'view'
const VIEWS = ['bot', 'room']

const KIND_ICON = {
  turn: 'comment-discussion',
  tool: 'tools',
  approval: 'shield',
  reasoning: 'lightbulb',
  message: 'comment',
  error: 'error',
  warning: 'warning'
}
const STATUS_VARIANT = {
  running: 'default',
  streaming: 'default',
  waiting: 'warn',
  ok: 'success',
  done: 'success',
  approved: 'success',
  error: 'destructive',
  deny: 'destructive',
  timeout: 'warn',
  cancelled: 'muted',
  warn: 'warn'
}

const EN = {
  title: 'Bot Activity',
  all: 'All bots',
  working: n => `${n} working`,
  idle: 'Idle',
  busy: 'working',
  seen: when => `Idle · ${when}`,
  never: 'No activity yet',
  emptyTitle: 'No activity yet',
  emptyBody: 'Bot turns, tool calls and approvals show up here as they happen.',
  clear: 'Clear log',
  clearFailed: 'Could not clear the activity log',
  turn: 'Turn',
  room: 'Group Chat',
  viewBot: 'By bot',
  viewRoom: 'By Group Chat',
  direct: 'Direct chats',
  deletedRoom: 'Deleted group',
  threadN: n => `Thread ${n}`,
  turns: n => (n === 1 ? '1 turn' : `${n} turns`),
  toggleSection: name => `Collapse or expand ${name}`,
  untracked: names => `${names} ${names.includes(',') ? "aren't" : "isn't"} tracked yet — activity won't show until you track ${names.includes(',') ? 'them' : 'it'}.`,
  outdatedOnly: names => `${names}: running an older version of this plugin.`,
  updated: 'Plugin updated. The new version runs after the next backend restart — restart when no bot is mid-task.',
  trackNow: 'Track now',
  syncNow: 'Update',
  tracking: 'Tracking…',
  tracked: n => (n === 1 ? 'Bot is now tracked. Activity shows from its next turn.' : `${n} bots are now tracked. Activity shows from their next turn.`),
  trackFailed: 'Could not track some bots',
  notTracked: 'not tracked',
  args: 'Arguments',
  result: 'Result',
  text: 'Output',
  stale: 'stale',
  chipTip: 'Open Bot Activity',
  offline: 'Activity backend unavailable — is the bot-activity plugin enabled in config.yaml?'
}

// ── Plugin-local state ────────────────────────────────────────────────────────
// Module scope survives hot reloads; register() resets it so a reload starts clean.
const $rows = atom(new Map())
const $filter = atom(ALL)
const $view = atom('bot')
// Room sections the user folded away (keyed by room id / DIRECT); runtime only.
const $collapsed = atom({})

let restRef = null
let persistView = null
// Poll cursor. ONLY pullSince advances it: pushes come from the Desktop backend process alone, so
// letting a pushed rev move the cursor would skip rows other processes (messaging gateway, cron,
// `hermes chat`) committed with lower revs in the meantime.
let cursor = 0
// Bumped by clear(); a pull that started before it must not resurrect deleted rows.
let epoch = 0

function mergeRows(incoming) {
  if (!incoming?.length) return
  const next = new Map($rows.get())
  for (const row of incoming) {
    const current = next.get(row.key)
    // A push captured before a newer poll result can arrive late; never step a row backwards.
    if (current && current.rev > row.rev) continue
    next.set(row.key, { ...(current || {}), ...row })
  }
  if (next.size > MAX_ROWS) {
    const oldest = [...next.values()].sort((a, b) => a.updated - b.updated).slice(0, next.size - MAX_ROWS)
    for (const row of oldest) next.delete(row.key)
  }
  $rows.set(next)
}

function resetFeed() {
  epoch += 1
  cursor = 0
  $rows.set(new Map())
}

async function pullSince() {
  if (!restRef) return null
  const startedAt = epoch
  const res = await restRef(`/activity?since=${cursor}&limit=1000`)
  if (startedAt !== epoch) return cursor
  if (res.rev < cursor) {
    // The backend renumbered its sequence (legacy migration) — start over from the top.
    resetFeed()
    return pullSince()
  }
  mergeRows(res.rows)
  for (const row of res.rows) if (row.rev > cursor) cursor = row.rev
  return cursor
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function useNow(intervalMs) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), intervalMs)
    return () => clearInterval(timer)
  }, [intervalMs])
  return now
}

function fmtDuration(ms) {
  if (ms == null || ms < 0) return ''
  if (ms < 1000) return `${Math.round(ms)}ms`
  const s = ms / 1000
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)}s`
  const m = Math.floor(s / 60)
  return m < 60 ? `${m}m ${Math.round(s % 60)}s` : `${Math.floor(m / 60)}h ${m % 60}m`
}

function isStale(row, now) {
  const quietFor = now - row.updated * 1000
  return row.status === 'streaming' ? quietFor > STREAM_QUIET_MS : ACTIVE.has(row.status) && quietFor > STALE_MS
}

/** A roster work item (from /bots) is live while its row keeps being updated — a long turn
 *  is kept fresh by the tool calls inside it. Older backends omit `updated`: fall back to `ts`. */
function isLiveItem(item, now) {
  return !isStale({ ...item, updated: item.updated ?? item.ts }, now)
}

function elapsed(row, now) {
  if (row.duration_ms != null) return row.duration_ms
  return ACTIVE.has(row.status) && !isStale(row, now) ? now - row.ts * 1000 : null
}

/** One-line preview of a tool's arguments: the command/path/query the user cares about. */
function argSummary(args) {
  if (args == null) return ''
  if (typeof args === 'string') return args
  for (const key of ['command', 'cmd', 'path', 'file_path', 'query', 'url', 'pattern', 'name', 'goal', 'message']) {
    if (typeof args[key] === 'string' && args[key]) return args[key]
  }
  const first = Object.values(args).find(v => typeof v === 'string' && v)
  return first || ''
}

function pretty(value) {
  if (value == null) return ''
  if (typeof value === 'string') return value
  return JSON.stringify(value, null, 2)
}

/** Group rows into turns: bot + turn id, newest activity first; a `turn` row becomes the header. */
function groupRows(rows, filter) {
  const groups = new Map()
  for (const row of rows) {
    const bot = row.bot || row.member_id || 'unknown'
    if (filter !== ALL && bot !== filter) continue
    const gid = row.turn_id ? `${bot}:${row.turn_id}` : `${bot}:${row.key}`
    let group = groups.get(gid)
    if (!group) {
      group = { id: gid, bot, head: null, items: [], last: 0, first: Infinity, room: null, thread: null }
      groups.set(gid, group)
    }
    if (row.kind === 'turn') group.head = row
    else group.items.push(row)
    group.last = Math.max(group.last, row.updated)
    group.first = Math.min(group.first, row.ts)
    group.room = group.room || row.room_id
    group.thread = group.thread || row.thread_id
  }
  const list = [...groups.values()].sort((a, b) => b.last - a.last).slice(0, MAX_GROUPS)
  for (const group of list) group.items.sort((a, b) => a.ts - b.ts)
  return list
}

/** Split turn groups into Group Chat sections (+ one for direct chats), busiest/latest first. */
function sectionGroups(groups, now) {
  const sections = new Map()
  for (const group of groups) {
    const id = group.room || DIRECT
    let section = sections.get(id)
    if (!section) {
      section = { id, groups: [], last: 0, working: new Set(), threadFirst: new Map() }
      sections.set(id, section)
    }
    section.groups.push(group)
    section.last = Math.max(section.last, group.last)
    if (groupRunning(group, now)) section.working.add(group.bot)
    if (group.thread) {
      section.threadFirst.set(group.thread, Math.min(section.threadFirst.get(group.thread) ?? Infinity, group.first))
    }
  }
  // "Thread N" numbering follows first appearance, so a label never changes as activity moves.
  for (const section of sections.values()) {
    section.threads = [...section.threadFirst].sort((a, b) => a[1] - b[1]).map(([thread]) => thread)
  }
  return [...sections.values()].sort((a, b) =>
    (b.working.size > 0) - (a.working.size > 0) || (a.id === DIRECT) - (b.id === DIRECT) || b.last - a.last)
}

function groupRunning(group, now) {
  const head = group.head
  return (head && ACTIVE.has(head.status) && !isStale(head, now)) ||
    group.items.some(r => ACTIVE.has(r.status) && !isStale(r, now))
}

// ── Data ─────────────────────────────────────────────────────────────────────

function useSetup() {
  return useQuery({
    queryKey: [ID, 'setup'],
    queryFn: () => restRef('/setup'),
    refetchInterval: SETUP_POLL_MS,
    retry: 1
  })
}

function useRoster() {
  return useQuery({
    queryKey: [ID, 'bots'],
    queryFn: () => restRef('/bots'),
    refetchInterval: ROSTER_POLL_MS,
    retry: 1
  })
}

/** Display names: Bot titles from the roster, Group Chat names + thread topics from /rooms.
 *  Every lookup falls back to the raw id so a missing mirror never blanks the UI. */
function useDirectory() {
  const roster = useRoster()
  const rooms = useQuery({
    queryKey: [ID, 'rooms'],
    queryFn: () => restRef('/rooms'),
    refetchInterval: ROOMS_POLL_MS,
    retry: 1
  })
  return useMemo(() => {
    const botTitles = new Map((roster.data?.bots || []).map(b => [b.bot, b.title]))
    const roomById = new Map((rooms.data?.rooms || []).map(r => [r.room_id, r]))
    return {
      bot: name => botTitles.get(name) || name,
      room: id => roomById.get(id),
      roomName: id => roomById.get(id)?.name || id,
      topic: (roomId, threadId) => roomById.get(roomId)?.threads?.[threadId]
    }
  }, [roster.data, rooms.data])
}

// ── Components ───────────────────────────────────────────────────────────────

function BotAvatar({ name, size = 8 }) {
  const color = profileColor(name)
  return jsx('span', {
    'aria-hidden': true,
    className: 'inline-block shrink-0 rounded-full',
    style: { width: size, height: size, background: color || 'var(--ui-accent)' }
  })
}

function StatusBadge({ status, stale }) {
  const t = usePluginI18n(ID)
  if (!status) return null
  // A quiet stream simply finished; any other in-progress row that went quiet was abandoned.
  if (stale && status === 'streaming') return jsx(Badge, { size: 'xs', variant: 'success', children: 'done' })
  return jsx(Badge, {
    size: 'xs',
    variant: stale ? 'muted' : STATUS_VARIANT[status] || 'muted',
    children: stale ? t('stale') : status
  })
}

function Roster({ bots, filter, now, untracked }) {
  const t = usePluginI18n(ID)
  const select = name => $filter.set(filter === name ? ALL : name)
  return jsxs('ul', {
    className: 'flex flex-col gap-0.5 px-2 py-1.5',
    'aria-label': t('title'),
    children: [
      jsx('li', {
        children: jsx('button', {
          type: 'button',
          onClick: () => $filter.set(ALL),
          'aria-pressed': filter === ALL,
          className: cn(
            'flex w-full items-center gap-2 rounded-md px-2 py-1 text-left text-xs transition-colors',
            'hover:bg-(--chrome-action-hover) focus-visible:outline focus-visible:outline-(--ui-accent)',
            filter === ALL ? 'bg-(--chrome-action-hover) text-foreground' : 'text-(--ui-text-secondary)'
          ),
          children: t('all')
        })
      }),
      ...bots.map(bot => {
        const working = bot.active.filter(a => isLiveItem(a, now))
        const current = working[working.length - 1]
        const line = current
          ? `${current.kind === 'tool' ? current.title : current.kind === 'turn' ? t('busy') : current.kind} · ${fmtDuration(now - current.ts * 1000)}`
          : bot.last_seen ? t('seen', relativeTime(bot.last_seen * 1000, now)) : t('never')
        return jsx('li', {
          children: jsxs('button', {
            type: 'button',
            onClick: () => select(bot.bot),
            'aria-pressed': filter === bot.bot,
            className: cn(
              'flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left transition-colors',
              'hover:bg-(--chrome-action-hover) focus-visible:outline focus-visible:outline-(--ui-accent)',
              filter === bot.bot && 'bg-(--chrome-action-hover)'
            ),
            children: [
              jsx(BotAvatar, { name: bot.bot }),
              jsxs('span', {
                className: 'flex min-w-0 flex-1 flex-col',
                children: [
                  jsxs('span', {
                    className: 'flex min-w-0 items-center gap-1.5',
                    children: [
                      jsx('span', { className: 'truncate text-xs font-medium text-foreground', children: bot.title || bot.bot }),
                      untracked.has(bot.bot) && jsx(Badge, { size: 'xs', variant: 'warn', children: t('notTracked') })
                    ]
                  }),
                  jsx('span', {
                    className: cn('truncate text-[0.6875rem]', current ? 'text-(--ui-accent)' : 'text-(--ui-text-tertiary)'),
                    children: line
                  })
                ]
              }),
              current
                ? jsx(Codicon, { name: 'loading', spinning: true, size: 12, className: 'text-(--ui-accent)' })
                : jsx(StatusDot, { tone: 'muted' })
            ]
          })
        }, bot.bot)
      })
    ]
  })
}

function DetailBlock({ label, value }) {
  const text = pretty(value)
  if (!text) return null
  return jsxs('div', {
    className: 'flex flex-col gap-1',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between text-[0.625rem] uppercase tracking-wide text-(--ui-text-quaternary)',
        children: [label, jsx(CopyButton, { text, appearance: 'icon', buttonSize: 'icon-xs' })]
      }),
      jsx('pre', {
        className: cn(
          'max-h-64 overflow-auto rounded-md border border-(--ui-stroke-secondary) px-2 py-1.5',
          'whitespace-pre-wrap break-words font-mono text-[0.6875rem] leading-relaxed text-(--ui-text-secondary)'
        ),
        children: text
      })
    ]
  })
}

function ActivityItem({ row, now }) {
  const t = usePluginI18n(ID)
  const [open, setOpen] = useState(false)
  const stale = isStale(row, now)
  const running = ACTIVE.has(row.status) && !stale
  const summary = row.kind === 'tool' ? argSummary(row.args)
    : row.kind === 'reasoning' || row.kind === 'message' ? (row.text || '').slice(-160)
    : row.title
  const label = row.kind === 'tool' ? row.title : row.kind
  const duration = fmtDuration(elapsed(row, now))
  const hasDetail = row.args != null || row.result != null || row.text

  return jsxs('li', {
    className: 'flex flex-col',
    children: [
      jsxs('button', {
        type: 'button',
        disabled: !hasDetail,
        'aria-expanded': hasDetail ? open : undefined,
        onClick: () => setOpen(v => !v),
        className: cn(
          'group flex w-full items-start gap-2 rounded-md px-1.5 py-1 text-left transition-colors',
          hasDetail && 'hover:bg-(--chrome-action-hover) focus-visible:outline focus-visible:outline-(--ui-accent)'
        ),
        children: [
          jsx(Codicon, {
            name: running ? 'loading' : KIND_ICON[row.kind] || 'circle-small',
            spinning: running,
            size: 13,
            className: cn('mt-0.5 shrink-0', row.status === 'error' ? 'text-destructive' : 'text-(--ui-text-tertiary)')
          }),
          jsxs('span', {
            className: 'flex min-w-0 flex-1 flex-col',
            children: [
              jsxs('span', {
                className: 'flex items-center gap-1.5',
                children: [
                  jsx('span', { className: 'truncate font-mono text-[0.6875rem] text-foreground', children: label }),
                  jsx(StatusBadge, { status: row.status, stale }),
                  duration && jsx('span', {
                    className: 'ml-auto shrink-0 tabular-nums text-[0.625rem] text-(--ui-text-quaternary)',
                    children: duration
                  })
                ]
              }),
              summary && jsx('span', {
                className: 'line-clamp-2 break-all text-[0.6875rem] text-(--ui-text-tertiary)',
                children: summary
              })
            ]
          })
        ]
      }),
      open && jsxs('div', {
        className: 'mb-1.5 mt-0.5 flex flex-col gap-2 pl-6',
        children: [
          jsx(DetailBlock, { label: t('args'), value: row.args }),
          jsx(DetailBlock, { label: t('result'), value: row.result }),
          jsx(DetailBlock, { label: t('text'), value: row.text })
        ]
      })
    ]
  })
}

function TurnGroup({ group, now, directory, inRoom = false, threadIndex = 0 }) {
  const t = usePluginI18n(ID)
  const head = group.head
  const running = groupRunning(group, now)
  const duration = head ? fmtDuration(elapsed(head, now)) : ''
  const [showReply, setShowReply] = useState(false)
  // Inside a room section the room is implied; label the thread instead (by its opening message).
  const topic = group.room && group.thread ? directory.topic(group.room, group.thread) : null
  const contextLabel = inRoom
    ? group.thread && (topic || t('threadN', threadIndex))
    : group.room && directory.roomName(group.room)

  return jsxs('section', {
    className: cn(
      'flex flex-col gap-0.5 rounded-lg border px-2 py-1.5 transition-colors',
      running ? 'border-primary/40' : 'border-(--ui-stroke-secondary)'
    ),
    children: [
      jsxs('header', {
        className: 'flex items-center gap-2 px-1',
        children: [
          jsx(BotAvatar, { name: group.bot }),
          jsx(Tip, {
            label: group.bot,
            children: jsx('span', {
              className: 'truncate text-xs font-medium text-foreground',
              children: directory.bot(group.bot)
            })
          }),
          contextLabel && jsx(Tip, {
            label: inRoom ? `${t('room')} · ${group.thread}` : `${t('room')} · ${group.room}`,
            children: jsx(Badge, {
              size: 'xs',
              variant: 'outline',
              className: 'min-w-0 max-w-48 truncate',
              children: inRoom ? contextLabel : `# ${contextLabel}`
            })
          }),
          jsx('span', {
            className: 'ml-auto shrink-0 tabular-nums text-[0.625rem] text-(--ui-text-quaternary)',
            children: [duration, relativeTime(group.last * 1000, now)].filter(Boolean).join(' · ')
          })
        ]
      }),
      head?.title && jsx('p', {
        className: 'line-clamp-2 px-1 text-[0.6875rem] italic text-(--ui-text-secondary)',
        children: head.title
      }),
      group.items.length > 0 && jsx('ul', {
        className: 'flex flex-col',
        children: group.items.map(row => jsx(ActivityItem, { row, now }, row.key))
      }),
      head?.text && jsxs('div', {
        className: 'px-1',
        children: [
          jsx('button', {
            type: 'button',
            onClick: () => setShowReply(v => !v),
            'aria-expanded': showReply,
            className: 'text-[0.625rem] uppercase tracking-wide text-(--ui-text-quaternary) hover:text-(--ui-text-secondary)',
            children: `${showReply ? '▾' : '▸'} ${t('text')}`
          }),
          showReply && jsx(DetailBlock, { label: '', value: head.text })
        ]
      })
    ]
  })
}

function RoomSection({ section, now, directory, collapsed }) {
  const t = usePluginI18n(ID)
  const isDirect = section.id === DIRECT
  const room = isDirect ? null : directory.room(section.id)
  const name = isDirect ? t('direct') : room?.name || t('deletedRoom')
  const members = room?.members?.length ? room.members : [...new Set(section.groups.map(g => g.bot))]
  const working = section.working.size
  const toggle = () => $collapsed.set({ ...$collapsed.get(), [section.id]: !collapsed })
  // Room ids may be legacy display names, and pane + page can mount together: use a React id.
  const bodyId = useId()

  return jsxs('section', {
    className: 'flex flex-col gap-1.5',
    children: [
      jsxs('button', {
        type: 'button',
        onClick: toggle,
        'aria-expanded': !collapsed,
        'aria-controls': bodyId,
        'aria-label': t('toggleSection', name),
        className: cn(
          'sticky top-0 z-10 flex w-full items-center gap-2 rounded-md bg-background px-2 py-1.5 text-left',
          'transition-colors hover:bg-(--chrome-action-hover) focus-visible:outline focus-visible:outline-(--ui-accent)'
        ),
        children: [
          jsx(Codicon, { name: collapsed ? 'chevron-right' : 'chevron-down', size: 12, className: 'text-(--ui-text-tertiary)' }),
          jsx(Codicon, {
            name: isDirect ? 'comment' : 'organization',
            size: 13,
            className: working > 0 ? 'text-(--ui-accent)' : 'text-(--ui-text-tertiary)'
          }),
          jsxs('span', {
            className: 'flex min-w-0 flex-1 flex-col',
            children: [
              jsx('span', { className: 'truncate text-xs font-semibold text-foreground', children: name }),
              jsx('span', {
                className: 'truncate text-[0.625rem] text-(--ui-text-quaternary)',
                children: [t('turns', section.groups.length), relativeTime(section.last * 1000, now)].join(' · ')
              })
            ]
          }),
          jsx('span', {
            className: 'flex shrink-0 items-center gap-1',
            'aria-hidden': true,
            children: members.slice(0, 6).map((m, i) => jsx(Tip, {
              label: directory.bot(m),
              children: jsx('span', {
                className: cn('rounded-full', section.working.has(m) ? 'ring-2 ring-primary/40' : 'opacity-60'),
                children: jsx(BotAvatar, { name: m, size: 8 })
              })
            }, `${m}:${i}`))
          }),
          working > 0 && jsx(Badge, { size: 'xs', children: t('working', working) })
        ]
      }),
      !collapsed && jsx('div', {
        id: bodyId,
        className: 'flex flex-col gap-2 border-l border-(--ui-stroke-secondary) pl-2 ml-3',
        children: section.groups.map(group => jsx(TurnGroup, {
          group,
          now,
          directory,
          inRoom: !isDirect,
          threadIndex: section.threads.indexOf(group.thread) + 1
        }, group.id))
      })
    ]
  })
}

/** Shown when a Bot profile lacks an enabled, current copy of the plugin (e.g. a Bot created
 *  after install). One click does what install.sh does, without a restart. */
function SetupBanner() {
  const t = usePluginI18n(ID)
  const setup = useSetup()
  const [progress, setProgress] = useState(null)
  const data = setup.data
  const untracked = data?.untracked || []
  // Outdated copies still record activity; only offer the update when nothing is untracked.
  const pending = untracked.length ? untracked : data?.outdated || []
  if (!pending.length) return null

  const titles = new Map((data.profiles || []).map(p => [p.bot, p.title || p.bot]))
  const names = pending.map(bot => titles.get(bot)).join(', ')
  const onlyOutdated = untracked.length === 0
  const track = async () => {
    const failed = []
    try {
      // One request per Bot: each runs `hermes plugins enable` (up to ~150 s server-side), so a
      // single request for many Bots would outlive any client timeout.
      for (const [index, bot] of pending.entries()) {
        setProgress(`${index + 1}/${pending.length}`)
        try {
          const res = await restRef('/setup', { method: 'POST', body: { bots: [bot] }, timeoutMs: TRACK_TIMEOUT_MS })
          for (const r of res.results || []) if (!r.ok) failed.push(`${titles.get(r.bot) || r.bot}: ${r.error}`)
        } catch (error) {
          failed.push(`${titles.get(bot) || bot}: ${error?.message || error}`)
        }
      }
      if (failed.length) host.notify({ kind: 'error', message: t('trackFailed'), detail: failed.join('\n') })
      else host.notify({ kind: 'success', message: onlyOutdated ? t('updated') : t('tracked', pending.length) })
    } finally {
      setProgress(null)
      queryClient.invalidateQueries({ queryKey: [ID, 'setup'] })
      queryClient.invalidateQueries({ queryKey: [ID, 'bots'] })
    }
  }

  return jsxs('div', {
    role: 'status',
    className: 'flex items-start gap-2 border-b border-(--ui-stroke-secondary) bg-amber-500/10 px-3 py-2',
    children: [
      jsx(Codicon, { name: onlyOutdated ? 'sync' : 'person-add', size: 14, className: 'mt-0.5 shrink-0 text-amber-600 dark:text-amber-300' }),
      jsx('p', {
        className: 'min-w-0 flex-1 text-[0.6875rem] leading-relaxed text-(--ui-text-secondary)',
        children: onlyOutdated ? t('outdatedOnly', names) : t('untracked', names)
      }),
      jsx(Button, {
        size: 'xs',
        variant: 'default',
        disabled: progress !== null,
        onClick: track,
        children: progress !== null
          ? [jsx(Codicon, { name: 'loading', spinning: true, size: 11 }, 'spin'), `${t('tracking')} ${progress}`]
          : onlyOutdated ? t('syncNow') : t('trackNow')
      })
    ]
  })
}

function ViewSwitch() {
  const t = usePluginI18n(ID)
  const view = useValue($view)
  return jsx(SegmentedControl, {
    className: 'w-full',
    options: [{ id: 'bot', label: t('viewBot') }, { id: 'room', label: t('viewRoom') }],
    value: view,
    onChange: next => {
      $view.set(next)
      persistView?.(next)
    }
  })
}

function ActivityView({ wide = false }) {
  const t = usePluginI18n(ID)
  const rowsMap = useValue($rows)
  const filter = useValue($filter)
  const view = useValue($view)
  const collapsed = useValue($collapsed)
  const now = useNow(1000)

  const feed = useQuery({
    queryKey: [ID, 'feed'],
    queryFn: pullSince,
    refetchInterval: POLL_MS,
    retry: 1
  })
  const roster = useRoster()
  const directory = useDirectory()
  const setup = useSetup()
  const untracked = useMemo(() => new Set(setup.data?.untracked || []), [setup.data])

  const groups = useMemo(() => groupRows(rowsMap.values(), filter), [rowsMap, filter])
  const sections = view === 'room' ? sectionGroups(groups, now) : null
  const bots = roster.data?.bots || []
  const working = bots.filter(b => b.active.some(a => isLiveItem(a, now))).length

  const clear = async () => {
    try {
      await restRef('/clear', { method: 'POST', body: filter === ALL ? {} : { bot: filter } })
    } catch (error) {
      host.notifyError(error, t('clearFailed'))
      return
    }
    epoch += 1
    $rows.set(new Map([...$rows.get()].filter(([, r]) => filter !== ALL && (r.bot || r.member_id) !== filter)))
    roster.refetch()
  }

  const header = jsxs('div', {
    className: 'flex items-center gap-2 border-b border-(--ui-stroke-secondary) px-3 py-2',
    children: [
      jsx(Codicon, { name: 'pulse', size: 14, className: 'text-(--ui-accent)' }),
      jsx('h2', { className: 'text-xs font-semibold text-foreground', children: t('title') }),
      working > 0 && jsx(Badge, { size: 'xs', children: t('working', working) }),
      jsx(Tip, {
        label: t('clear'),
        children: jsx(Button, {
          size: 'icon-xs',
          variant: 'ghost',
          className: 'ml-auto',
          'aria-label': t('clear'),
          onClick: clear,
          children: jsx(Codicon, { name: 'clear-all', size: 13 })
        })
      })
    ]
  })

  const timeline = feed.isError && rowsMap.size === 0
    ? jsx('p', { className: 'p-4 text-xs text-destructive', children: t('offline') })
    : groups.length === 0
      ? jsx(EmptyState, { title: t('emptyTitle'), description: t('emptyBody'), className: 'py-10' })
      : sections
        ? jsx('div', {
          className: 'flex flex-col gap-3 p-2',
          children: sections.map(section => jsx(RoomSection, {
            section, now, directory, collapsed: Boolean(collapsed[section.id])
          }, section.id))
        })
        : jsx('div', {
          className: 'flex flex-col gap-2 p-2',
          children: groups.map(group => jsx(TurnGroup, { group, now, directory }, group.id))
        })

  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col text-sm',
    children: [
      header,
      jsx(SetupBanner, {}),
      jsx('div', { className: 'border-b border-(--ui-stroke-secondary) px-3 py-1.5', children: jsx(ViewSwitch, {}) }),
      jsxs('div', {
        className: cn('flex min-h-0 flex-1', wide ? 'flex-row' : 'flex-col'),
        children: [
          jsx('div', {
            className: cn(
              'shrink-0 overflow-auto border-(--ui-stroke-secondary)',
              wide ? 'w-60 border-r' : 'max-h-[40vh] border-b'
            ),
            children: jsx(Roster, { bots, filter, now, untracked })
          }),
          jsx(ScrollArea, { className: 'min-h-0 flex-1', children: timeline })
        ]
      })
    ]
  })
}

function StatusChip() {
  const t = usePluginI18n(ID)
  const roster = useRoster()
  const now = Date.now()
  const working = (roster.data?.bots || []).filter(b => b.active.some(a => isLiveItem(a, now))).length
  return jsx(Tip, {
    label: t('chipTip'),
    children: jsxs('button', {
      type: 'button',
      onClick: () => host.navigate(ROUTE),
      className: cn(
        'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem] transition-colors',
        'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground'
      ),
      children: [
        working > 0
          ? jsx(Codicon, { name: 'loading', spinning: true, size: 11, className: 'text-(--ui-accent)' })
          : jsx(Codicon, { name: 'pulse', size: 11 }),
        working > 0 ? t('working', working) : t('idle')
      ]
    })
  })
}

export default {
  id: ID,
  name: 'Bot Activity',
  register(ctx) {
    restRef = ctx.rest
    resetFeed()
    const storedView = ctx.storage.get(VIEW_STORAGE_KEY, 'bot')
    $view.set(VIEWS.includes(storedView) ? storedView : 'bot')
    persistView = next => ctx.storage.set(VIEW_STORAGE_KEY, next)

    ctx.i18n.register({ en: EN })

    ctx.onEvent(EVENT, frame => {
      const payload = frame?.payload || {}
      if (payload.refetch) pullSince().catch(() => {})
      else mergeRows(payload.rows)
    })

    ctx.registerMany([
      {
        id: 'pane',
        area: 'panes',
        title: 'bot activity',
        data: { placement: 'right', width: '380px' },
        render: () => jsx(ActivityView, {})
      },
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: ROUTE },
        render: () => jsx(ActivityView, { wide: true })
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        data: { path: ROUTE, label: 'Bot Activity', codicon: 'pulse' }
      },
      {
        id: 'chip',
        area: 'statusBar.right',
        order: 125,
        render: () => jsx(StatusChip, {})
      }
    ])

    ctx.onDispose(() => {
      restRef = null
      persistView = null
    })
  }
}
