import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  Alert,
  Avatar,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  Collapse,
  Container,
  Divider,
  IconButton,
  LinearProgress,
  Link,
  Paper,
  Stack,
  Step,
  StepLabel,
  Stepper,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'

type UiStatus = 'idle' | 'running' | 'done' | 'error'

interface RunSummary {
  window?: number
  rules?: number
  llm?: number
  sent?: number
  sources?: number
  new_greenhouse?: number
  new_lever?: number
  email_sent?: boolean
  line?: string
}

interface ProgressEvent {
  phase?: string
  message?: string
  ts?: number
  source?: string
  jobs_found?: number
  raw_jobs?: number
  jobs_after_window?: number
  jobs_after_rules?: number
  jobs_to_score?: number
  jobs_matched?: number
}

interface StreamEvent {
  type: 'start' | 'progress' | 'done' | 'error'
  run_id?: string
  summary?: RunSummary
  email_sent?: boolean
  download_available?: boolean
  error?: string
  phase?: string
  message?: string
  ts?: number
  source?: string
  jobs_found?: number
  tier?: 'owner' | 'byok' | 'free'
  enabled_sources?: string[]
  gate?: { ip_count: number; ip_cap: number; global_count: number; global_cap: number }
  /** Fired when server has stored a partial HTML report (pre-LLM). */
  partial_download_available?: boolean
  /** True when continuing from a server-side checkpoint (same run_id). */
  resuming?: boolean
}

interface Byok {
  groq: string
  gemini: string
  openrouter: string
  serpapi: string
}

const BYOK_STORAGE_KEY = 'job_hunter_byok_v1'
/** Last failed UI run_id — server may still have a pipeline checkpoint to resume. */
const RESUME_RUN_STASH_KEY = 'job_hunter_resume_run_id_v1'
const EMPTY_BYOK: Byok = { groq: '', gemini: '', openrouter: '', serpapi: '' }

function loadByok(): Byok {
  try {
    const raw = localStorage.getItem(BYOK_STORAGE_KEY)
    if (!raw) return EMPTY_BYOK
    const parsed = JSON.parse(raw) as Partial<Byok>
    return { ...EMPTY_BYOK, ...parsed }
  } catch {
    return EMPTY_BYOK
  }
}

function saveByok(b: Byok) {
  try {
    localStorage.setItem(BYOK_STORAGE_KEY, JSON.stringify(b))
  } catch {
    /* noop */
  }
}

/** Last completed run metadata (small JSON). */
const LAST_RUN_META_KEY = 'job_hunter_last_run_meta_v1'
/** Raw HTML for the report; avoids doubling size via JSON escaping. */
const LAST_RUN_HTML_KEY = 'job_hunter_last_report_html_v1'
/** run_id that `LAST_RUN_HTML_KEY` belongs to (invalidates stale downloads). */
const LAST_RUN_HTML_RUN_ID_KEY = 'job_hunter_last_report_run_id_v1'

interface LastRunCacheMeta {
  v: 1
  savedAt: string
  runId: string
  summary: RunSummary
  emailSent: boolean
  downloadAvailable: boolean
  tier: 'owner' | 'byok' | 'free' | null
  email: string
}

function loadLastRunMeta(): LastRunCacheMeta | null {
  try {
    const raw = localStorage.getItem(LAST_RUN_META_KEY)
    if (!raw) return null
    const p = JSON.parse(raw) as LastRunCacheMeta
    if (p?.v !== 1 || !p.runId || !p.savedAt) return null
    return p
  } catch {
    return null
  }
}

function saveLastRunMeta(m: LastRunCacheMeta) {
  try {
    localStorage.setItem(LAST_RUN_META_KEY, JSON.stringify(m))
  } catch {
    /* noop */
  }
}

function loadLastReportHtml(): string | null {
  try {
    return localStorage.getItem(LAST_RUN_HTML_KEY)
  } catch {
    return null
  }
}

function loadCachedHtmlRunId(): string | null {
  try {
    return localStorage.getItem(LAST_RUN_HTML_RUN_ID_KEY)
  } catch {
    return null
  }
}

function clearSavedReportHtml() {
  try {
    localStorage.removeItem(LAST_RUN_HTML_KEY)
    localStorage.removeItem(LAST_RUN_HTML_RUN_ID_KEY)
  } catch {
    /* noop */
  }
}

function clearLastRunCache() {
  try {
    localStorage.removeItem(LAST_RUN_META_KEY)
    clearSavedReportHtml()
  } catch {
    /* noop */
  }
}

function triggerDownloadHtmlBlob(html: string, filename: string) {
  const blob = new Blob([html], { type: 'text/html;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.rel = 'noopener'
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

/** Fetch report HTML and store under localStorage; returns whether bytes were stored. */
async function fetchAndStoreReportHtml(runId: string): Promise<boolean> {
  try {
    const r = await fetch(`/api/runs/${runId}/report.html`)
    if (!r.ok) return false
    const html = await r.text()
    try {
      localStorage.setItem(LAST_RUN_HTML_KEY, html)
      localStorage.setItem(LAST_RUN_HTML_RUN_ID_KEY, runId)
      return true
    } catch {
      return false
    }
  } catch {
    return false
  }
}

function byokHeaders(b: Byok): Record<string, string> {
  const h: Record<string, string> = {}
  if (b.groq.trim()) h['x-llm-groq-key'] = b.groq.trim()
  if (b.gemini.trim()) h['x-llm-gemini-key'] = b.gemini.trim()
  if (b.openrouter.trim()) h['x-llm-openrouter-key'] = b.openrouter.trim()
  if (b.serpapi.trim()) h['x-serpapi-key'] = b.serpapi.trim()
  return h
}

function byokIsSet(b: Byok): boolean {
  return Boolean(
    b.groq.trim() || b.gemini.trim() || b.openrouter.trim() || b.serpapi.trim(),
  )
}

async function* readSseStream(
  res: Response,
  signal: AbortSignal,
): AsyncGenerator<StreamEvent> {
  if (!res.body) throw new Error('No response body (SSE unsupported).')
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  try {
    while (true) {
      if (signal.aborted) return
      const { done, value } = await reader.read()
      if (done) return
      buf += decoder.decode(value, { stream: true })
      // SSE messages are separated by blank lines.
      let idx: number
      while ((idx = buf.indexOf('\n\n')) !== -1) {
        const frame = buf.slice(0, idx)
        buf = buf.slice(idx + 2)
        // Each frame has one or more lines; we care about `data:` lines.
        const dataLines = frame
          .split('\n')
          .filter((l) => l.startsWith('data:'))
          .map((l) => l.slice(5).trimStart())
        if (dataLines.length === 0) continue // comment / keepalive
        const payload = dataLines.join('\n')
        try {
          yield JSON.parse(payload) as StreamEvent
        } catch {
          // ignore malformed frames
        }
      }
    }
  } finally {
    try {
      reader.cancel()
    } catch {
      /* noop */
    }
  }
}

const PHASES = ['Fetch', 'Filter', 'Score', 'Email'] as const

/** Display version (aligned with job-hunter package). */
const APP_VERSION = '0.1.0'

function UploadDocIcon() {
  return (
    <Box
      component="svg"
      viewBox="0 0 24 24"
      sx={{ width: 44, height: 44, color: 'primary.main' }}
      aria-hidden
    >
      <path
        fill="currentColor"
        d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6Zm4 18H6V4h7v5h5v11ZM8 12h8v2H8v-2Zm0 4h8v2H8v-2Z"
      />
    </Box>
  )
}

function FieldLabel({ children }: { children: ReactNode }) {
  return (
    <Typography
      variant="caption"
      component="span"
      sx={{
        display: 'block',
        mb: 0.75,
        fontWeight: 600,
        letterSpacing: '0.08em',
        textTransform: 'uppercase',
        color: 'text.secondary',
        fontSize: '0.7rem',
      }}
    >
      {children}
    </Typography>
  )
}

function linesToList(s: string): string[] {
  return s
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean)
}

function autoSlug(name: string): string {
  const base = name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 24)
  const suffix = Math.random().toString(36).slice(2, 6)
  return base ? `${base}-${suffix}` : `run-${suffix}`
}

function activeStep(progress: ProgressEvent[]): number {
  if (progress.length === 0) return 0
  const phases = new Set(progress.map((p) => (p.phase || '').toLowerCase()))
  if (phases.has('email') || phases.has('done')) return 3
  if (phases.has('score') || phases.has('llm')) return 2
  if (phases.has('filter') || phases.has('rules') || phases.has('window')) return 1
  return 0
}

const DEFAULTS = {
  target: ['AI Engineer', 'Data Scientist', 'Machine Learning Engineer'].join('\n'),
  related: [
    'Applied Scientist',
    'ML Research Engineer',
    'NLP Engineer',
    'GenAI Engineer',
    'LLM Engineer',
    'ML Engineer',
  ].join('\n'),
  locations: ['India', 'Remote', 'Bangalore', 'Bengaluru', 'Hyderabad'].join('\n'),
  reject: ['Intern', 'Principal', 'Director', 'VP', 'Head of', 'Staff', 'Chief'].join('\n'),
}

export default function App() {
  const [email, setEmail] = useState('')
  const [name, setName] = useState('')
  const [resumeFile, setResumeFile] = useState<File | null>(null)
  const [dragOver, setDragOver] = useState(false)

  const [targetTitles, setTargetTitles] = useState(DEFAULTS.target)
  const [relatedTitles, setRelatedTitles] = useState(DEFAULTS.related)
  const [locations, setLocations] = useState(DEFAULTS.locations)
  const [minYears, setMinYears] = useState(1)
  const [maxYears, setMaxYears] = useState(3)
  const [rejectTitles, setRejectTitles] = useState(DEFAULTS.reject)

  const [uiStatus, setUiStatus] = useState<UiStatus>('idle')
  const [summary, setSummary] = useState<RunSummary | null>(null)
  const [emailSent, setEmailSent] = useState(false)
  const [downloadAvailable, setDownloadAvailable] = useState(false)
  const [partialDownloadAvailable, setPartialDownloadAvailable] = useState(false)
  const [currentRunId, setCurrentRunId] = useState<string | null>(null)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [progress, setProgress] = useState<ProgressEvent[]>([])
  const [byok, setByokState] = useState<Byok>(EMPTY_BYOK)
  const [byokOpen, setByokOpen] = useState(false)
  const [serverTier, setServerTier] = useState<'owner' | 'byok' | 'free' | null>(null)
  const [gateInfo, setGateInfo] = useState<StreamEvent['gate'] | null>(null)
  const [lastRunCache, setLastRunCache] = useState<LastRunCacheMeta | null>(() =>
    loadLastRunMeta(),
  )
  const [hasCachedReportHtml, setHasCachedReportHtml] = useState(() => {
    const meta = loadLastRunMeta()
    const rid = loadCachedHtmlRunId()
    return Boolean(
      meta && rid && meta.runId === rid && loadLastReportHtml(),
    )
  })
  const [browserReportSaved, setBrowserReportSaved] = useState(false)

  useEffect(() => {
    setByokState(loadByok())
  }, [])

  useEffect(() => {
    try {
      setResumeStashedRunId(sessionStorage.getItem(RESUME_RUN_STASH_KEY) ?? '')
    } catch {
      setResumeStashedRunId('')
    }
  }, [])

  function setByok(next: Byok) {
    setByokState(next)
    saveByok(next)
  }

  const hasByok = byokIsSet(byok)

  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  /** Latest run_id from SSE `start` (ref avoids stale state when an error arrives). */
  const lastRunIdRef = useRef<string | null>(null)
  const slug = useMemo(() => autoSlug(name || 'run'), [name])

  const [resumeStashedRunId, setResumeStashedRunId] = useState('')

  function resetRun() {
    abortRef.current?.abort()
    abortRef.current = null
    lastRunIdRef.current = null
    setUiStatus('idle')
    setSummary(null)
    setEmailSent(false)
    setDownloadAvailable(false)
    setPartialDownloadAvailable(false)
    setCurrentRunId(null)
    setErrorMessage(null)
    setProgress([])
    setServerTier(null)
    setGateInfo(null)
    setBrowserReportSaved(false)
  }

  function handleDrop(e: React.DragEvent<HTMLDivElement>) {
    e.preventDefault()
    setDragOver(false)
    const f = e.dataTransfer.files?.[0]
    if (f) setResumeFile(f)
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    resetRun()

    if (!email.trim()) {
      setErrorMessage('Enter the email address that should receive results.')
      return
    }
    if (!name.trim()) {
      setErrorMessage('Enter your name.')
      return
    }
    if (!resumeFile) {
      setErrorMessage('Upload your resume (Markdown, plain text, or PDF).')
      return
    }
    const target = linesToList(targetTitles)
    if (target.length === 0) {
      setErrorMessage('Add at least one target job title.')
      return
    }

    const profile = {
      name: name.trim(),
      target_titles: target,
      related_titles: linesToList(relatedTitles),
      locations: linesToList(locations),
      seniority: {
        min_years: minYears,
        max_years: maxYears,
        reject_if_title_contains: linesToList(rejectTitles),
      },
    }

    setSubmitting(true)
    setUiStatus('running')
    const ctrl = new AbortController()
    abortRef.current = ctrl

    try {
      const fd = new FormData()
      fd.append('email', email.trim())
      fd.append('slug', slug)
      fd.append('resume', resumeFile)
      fd.append('profile_json', JSON.stringify(profile))

      const res = await fetch('/api/run/stream', {
        method: 'POST',
        body: fd,
        signal: ctrl.signal,
        headers: byokHeaders(byok),
      })

      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        let detail = res.statusText
        if (typeof data === 'object' && data && 'detail' in data) {
          const d = (data as { detail: unknown }).detail
          detail = typeof d === 'string' ? d : JSON.stringify(d)
        }
        setErrorMessage(detail)
        setUiStatus('error')
        return
      }

      setSubmitting(false)
      for await (const ev of readSseStream(res, ctrl.signal)) {
        if (ev.type === 'start' && ev.run_id) {
          lastRunIdRef.current = ev.run_id
          setCurrentRunId(ev.run_id)
          if (ev.tier) setServerTier(ev.tier)
          if (ev.gate) setGateInfo(ev.gate)
        } else if (ev.type === 'progress') {
          if (ev.partial_download_available) {
            setPartialDownloadAvailable(true)
          }
          setProgress((prev) => {
            const next = [...prev, ev as ProgressEvent]
            return next.length > 200 ? next.slice(-160) : next
          })
        } else if (ev.type === 'done') {
          setSummary(ev.summary ?? null)
          setEmailSent(Boolean(ev.email_sent))
          setDownloadAvailable(Boolean(ev.download_available))
          if (ev.run_id) setCurrentRunId(ev.run_id)
          setUiStatus('done')
          if (ev.run_id && ev.summary) {
            const meta: LastRunCacheMeta = {
              v: 1,
              savedAt: new Date().toISOString(),
              runId: ev.run_id,
              summary: ev.summary,
              emailSent: Boolean(ev.email_sent),
              downloadAvailable: Boolean(ev.download_available),
              tier: serverTier,
              email: email.trim(),
            }
            saveLastRunMeta(meta)
            setLastRunCache(meta)
            if (ev.download_available) {
              void fetchAndStoreReportHtml(ev.run_id).then((ok) => {
                setBrowserReportSaved(ok)
                setHasCachedReportHtml(ok && loadCachedHtmlRunId() === ev.run_id)
              })
            } else {
              clearSavedReportHtml()
              setHasCachedReportHtml(false)
              setBrowserReportSaved(false)
            }
          }
          try {
            sessionStorage.removeItem(RESUME_RUN_STASH_KEY)
          } catch {
            /* noop */
          }
          setResumeStashedRunId('')
          lastRunIdRef.current = null
          return
        } else if (ev.type === 'error') {
          const rid = lastRunIdRef.current
          if (rid) {
            try {
              sessionStorage.setItem(RESUME_RUN_STASH_KEY, rid)
            } catch {
              /* noop */
            }
            setResumeStashedRunId(rid)
          }
          setErrorMessage(ev.error ?? 'Run failed.')
          setUiStatus('error')
          return
        }
      }
      // Stream ended without terminal event (connection dropped).
      if (uiStatus !== 'done' && uiStatus !== 'error') {
        const rid = lastRunIdRef.current
        if (rid) {
          try {
            sessionStorage.setItem(RESUME_RUN_STASH_KEY, rid)
          } catch {
            /* noop */
          }
          setResumeStashedRunId(rid)
        }
        setErrorMessage(
          'Connection closed before the run finished. The run may still be ' +
            'running on the server; check your email.',
        )
        setUiStatus('error')
      }
    } catch (err) {
      if ((err as Error).name === 'AbortError') return
      const rid = lastRunIdRef.current
      if (rid) {
        try {
          sessionStorage.setItem(RESUME_RUN_STASH_KEY, rid)
        } catch {
          /* noop */
        }
        setResumeStashedRunId(rid)
      }
      setUiStatus('error')
      setErrorMessage(err instanceof Error ? err.message : String(err))
    } finally {
      setSubmitting(false)
    }
  }

  async function handleResumeFromCheckpoint() {
    const rid =
      resumeStashedRunId.trim() ||
      (typeof sessionStorage !== 'undefined'
        ? sessionStorage.getItem(RESUME_RUN_STASH_KEY) ?? ''
        : '')
    if (!rid) {
      setErrorMessage('No saved run id to resume.')
      return
    }
    if (!email.trim()) {
      setErrorMessage('Enter the same email address you used for the failed run.')
      return
    }

    abortRef.current?.abort()
    abortRef.current = null
    setErrorMessage(null)
    setProgress([])
    setSummary(null)
    setEmailSent(false)
    setDownloadAvailable(false)
    setBrowserReportSaved(false)
    setSubmitting(true)
    setUiStatus('running')
    const ctrl = new AbortController()
    abortRef.current = ctrl

    try {
      const fd = new FormData()
      fd.append('email', email.trim())
      fd.append('run_id', rid)

      const res = await fetch('/api/run/stream/resume', {
        method: 'POST',
        body: fd,
        signal: ctrl.signal,
        headers: byokHeaders(byok),
      })

      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        let detail = res.statusText
        if (typeof data === 'object' && data && 'detail' in data) {
          const d = (data as { detail: unknown }).detail
          detail = typeof d === 'string' ? d : JSON.stringify(d)
        }
        setErrorMessage(detail)
        setUiStatus('error')
        return
      }

      setSubmitting(false)
      for await (const ev of readSseStream(res, ctrl.signal)) {
        if (ev.type === 'start' && ev.run_id) {
          lastRunIdRef.current = ev.run_id
          setCurrentRunId(ev.run_id)
          if (ev.tier) setServerTier(ev.tier)
          if (ev.gate) setGateInfo(ev.gate)
        } else if (ev.type === 'progress') {
          if (ev.partial_download_available) {
            setPartialDownloadAvailable(true)
          }
          setProgress((prev) => {
            const next = [...prev, ev as ProgressEvent]
            return next.length > 200 ? next.slice(-160) : next
          })
        } else if (ev.type === 'done') {
          setSummary(ev.summary ?? null)
          setEmailSent(Boolean(ev.email_sent))
          setDownloadAvailable(Boolean(ev.download_available))
          if (ev.run_id) setCurrentRunId(ev.run_id)
          setUiStatus('done')
          if (ev.run_id && ev.summary) {
            const meta: LastRunCacheMeta = {
              v: 1,
              savedAt: new Date().toISOString(),
              runId: ev.run_id,
              summary: ev.summary,
              emailSent: Boolean(ev.email_sent),
              downloadAvailable: Boolean(ev.download_available),
              tier: serverTier,
              email: email.trim(),
            }
            saveLastRunMeta(meta)
            setLastRunCache(meta)
            if (ev.download_available) {
              void fetchAndStoreReportHtml(ev.run_id).then((ok) => {
                setBrowserReportSaved(ok)
                setHasCachedReportHtml(ok && loadCachedHtmlRunId() === ev.run_id)
              })
            } else {
              clearSavedReportHtml()
              setHasCachedReportHtml(false)
              setBrowserReportSaved(false)
            }
          }
          try {
            sessionStorage.removeItem(RESUME_RUN_STASH_KEY)
          } catch {
            /* noop */
          }
          setResumeStashedRunId('')
          lastRunIdRef.current = null
          return
        } else if (ev.type === 'error') {
          const erid = lastRunIdRef.current
          if (erid) {
            try {
              sessionStorage.setItem(RESUME_RUN_STASH_KEY, erid)
            } catch {
              /* noop */
            }
            setResumeStashedRunId(erid)
          }
          setErrorMessage(ev.error ?? 'Resume failed.')
          setUiStatus('error')
          return
        }
      }
      const erid = lastRunIdRef.current
      if (erid) {
        try {
          sessionStorage.setItem(RESUME_RUN_STASH_KEY, erid)
        } catch {
          /* noop */
        }
        setResumeStashedRunId(erid)
      }
      setErrorMessage(
        'Connection closed before resume finished. Try Resume again if a checkpoint still exists.',
      )
      setUiStatus('error')
    } catch (err) {
      if ((err as Error).name === 'AbortError') return
      const erid = lastRunIdRef.current
      if (erid) {
        try {
          sessionStorage.setItem(RESUME_RUN_STASH_KEY, erid)
        } catch {
          /* noop */
        }
        setResumeStashedRunId(erid)
      }
      setUiStatus('error')
      setErrorMessage(err instanceof Error ? err.message : String(err))
    } finally {
      setSubmitting(false)
    }
  }

  const busy = submitting || uiStatus === 'running'
  const step = activeStep(progress)
  const lastMessage =
    progress.length > 0
      ? progress[progress.length - 1].message ??
        `${progress[progress.length - 1].phase ?? ''} ${
          progress[progress.length - 1].source ?? ''
        }`.trim()
      : 'Starting…'

  const avatarLetter = name.trim().charAt(0).toUpperCase() || '·'

  return (
    <Box
      sx={{
        minHeight: '100vh',
        display: 'flex',
        flexDirection: 'column',
        bgcolor: 'background.default',
      }}
    >
      <Box
        component="header"
        sx={{
          px: { xs: 2, sm: 3 },
          py: 2,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          borderBottom: '1px solid',
          borderColor: 'divider',
          bgcolor: 'background.paper',
        }}
      >
        <Typography
          variant="h6"
          component="span"
          sx={{ fontWeight: 700, letterSpacing: '-0.02em', color: 'primary.dark' }}
        >
          Job Hunter
        </Typography>
        <Avatar
          sx={{
            width: 36,
            height: 36,
            bgcolor: 'primary.main',
            fontSize: '0.95rem',
            fontWeight: 600,
          }}
          aria-hidden
        >
          {avatarLetter}
        </Avatar>
      </Box>

      <Container maxWidth="md" sx={{ py: { xs: 3, sm: 5 }, flex: 1 }}>
        {uiStatus === 'running' ? (
          <RunningCard step={step} lastMessage={lastMessage} progress={progress} />
        ) : uiStatus === 'done' ? (
          <DoneCard
            summary={summary}
            emailSent={emailSent}
            downloadAvailable={downloadAvailable}
            runId={currentRunId}
            email={email}
            tier={serverTier}
            onAgain={resetRun}
            browserReportSaved={browserReportSaved}
          />
        ) : (
          <>
            <Typography variant="h4" component="h1" gutterBottom>
              Refine your search
            </Typography>
            {lastRunCache && (
              <LastRunCacheCard
                meta={lastRunCache}
                hasReportHtml={hasCachedReportHtml}
                onDownloadCached={() => {
                  const html = loadLastReportHtml()
                  const rid = loadCachedHtmlRunId()
                  if (!html || rid !== lastRunCache.runId) return
                  triggerDownloadHtmlBlob(html, 'job-hunter-report.html')
                }}
                onForget={() => {
                  clearLastRunCache()
                  setLastRunCache(null)
                  setHasCachedReportHtml(false)
                }}
              />
            )}

            {resumeStashedRunId ? (
              <Alert
                severity="warning"
                sx={{ mb: 3, borderRadius: 2 }}
                action={
                  <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap' }}>
                    <Button
                      size="small"
                      variant="contained"
                      disabled={busy || !email.trim()}
                      onClick={() => void handleResumeFromCheckpoint()}
                    >
                      Resume from checkpoint
                    </Button>
                    <Button
                      size="small"
                      onClick={() => {
                        try {
                          sessionStorage.removeItem(RESUME_RUN_STASH_KEY)
                        } catch {
                          /* noop */
                        }
                        setResumeStashedRunId('')
                      }}
                    >
                      Dismiss
                    </Button>
                  </Stack>
                }
              >
                A recent run stopped with an error. Run id{' '}
                <Box component="span" sx={{ fontFamily: 'monospace', fontSize: '0.85em' }}>
                  {resumeStashedRunId}
                </Box>
                . If this browser is on the same machine as the server and the run folder
                still exists, you can continue from the last saved pipeline step (no new
                upload). Use the same email as before.{' '}
                <Link href={`/api/run/checkpoint/${encodeURIComponent(resumeStashedRunId)}`}>
                  Check checkpoint
                </Link>
              </Alert>
            ) : null}

            <Typography variant="subtitle1" sx={{ mb: 4, maxWidth: 560 }}>
              Fill in your preferences to run the pipeline: job boards, rules, and
              LLM scoring against your resume—one run per click.{' '}
              <Tooltip
                arrow
                title="This UI triggers ad-hoc runs. For twice-a-day scheduled runs, use the GitHub Actions workflow in the repo."
              >
                <Link component="button" type="button" underline="hover" sx={{ verticalAlign: 'baseline' }}>
                  Scheduled runs?
                </Link>
              </Tooltip>
            </Typography>

            <Box component="form" onSubmit={handleSubmit} noValidate>
              <Stack spacing={3.5}>
                <TierBanner
                  hasByok={hasByok}
                  onOpenByok={() => setByokOpen(true)}
                  gateInfo={gateInfo}
                />
                <ByokSection
                  open={byokOpen}
                  onToggle={() => setByokOpen((v) => !v)}
                  byok={byok}
                  onChange={setByok}
                  onClear={() => setByok(EMPTY_BYOK)}
                  disabled={busy}
                />

                <Section title="About you">
                  <Stack
                    direction={{ xs: 'column', sm: 'row' }}
                    spacing={2}
                    sx={{ alignItems: 'stretch' }}
                  >
                    <Box sx={{ flex: 1 }}>
                      <FieldLabel>Full name</FieldLabel>
                      <TextField
                        value={name}
                        onChange={(e) => setName(e.target.value)}
                        required
                        fullWidth
                        disabled={busy}
                        placeholder="Your name"
                        hiddenLabel
                      />
                    </Box>
                    <Box sx={{ flex: 1 }}>
                      <FieldLabel>Email</FieldLabel>
                      <TextField
                        type="email"
                        value={email}
                        onChange={(e) => setEmail(e.target.value)}
                        required
                        fullWidth
                        disabled={busy}
                        placeholder="you@example.com"
                        hiddenLabel
                      />
                    </Box>
                  </Stack>
                  <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: -0.5 }}>
                    If this matches JOB_HUNTER_UI_OWNER_EMAIL on the server and SMTP is
                    configured, you get a real email. Otherwise you get an HTML download
                    (safe for guests).
                  </Typography>

                  <Box sx={{ mt: 1 }}>
                    <FieldLabel>Resume / CV</FieldLabel>
                    <DropZone
                      file={resumeFile}
                      dragOver={dragOver}
                      onDragEnter={() => setDragOver(true)}
                      onDragLeave={() => setDragOver(false)}
                      onDrop={handleDrop}
                      onClick={() => fileInputRef.current?.click()}
                      onClear={() => setResumeFile(null)}
                      disabled={busy}
                    />
                    <input
                      ref={fileInputRef}
                      type="file"
                      hidden
                      accept=".md,.txt,.pdf,text/markdown,text/plain,application/pdf"
                      onChange={(e) => setResumeFile(e.target.files?.[0] ?? null)}
                    />
                  </Box>
                </Section>

                <Section
                  title="What you're looking for"
                  hint="One per line. Related titles help the scorer match adjacent roles (e.g. ML Engineer for an AI Engineer)."
                >
                  <Box>
                    <FieldLabel>Target job titles</FieldLabel>
                    <TextField
                      value={targetTitles}
                      onChange={(e) => setTargetTitles(e.target.value)}
                      required
                      fullWidth
                      multiline
                      minRows={3}
                      disabled={busy}
                      placeholder="e.g. AI Engineer"
                      hiddenLabel
                    />
                  </Box>
                  <Box>
                    <FieldLabel>Related titles (optional)</FieldLabel>
                    <TextField
                      value={relatedTitles}
                      onChange={(e) => setRelatedTitles(e.target.value)}
                      fullWidth
                      multiline
                      minRows={3}
                      disabled={busy}
                      hiddenLabel
                    />
                  </Box>
                  <Box>
                    <FieldLabel>Locations</FieldLabel>
                    <TextField
                      value={locations}
                      onChange={(e) => setLocations(e.target.value)}
                      fullWidth
                      multiline
                      minRows={2}
                      disabled={busy}
                      placeholder="Cities, countries, or Remote — one per line"
                      hiddenLabel
                    />
                  </Box>
                </Section>

                <Section title="Seniority guardrails">
                  <Stack direction="row" spacing={2} sx={{ flexWrap: 'wrap' }}>
                    <Box sx={{ width: { xs: '100%', sm: 140 } }}>
                      <FieldLabel>Min years</FieldLabel>
                      <TextField
                        type="number"
                        value={minYears}
                        onChange={(e) => setMinYears(Number(e.target.value))}
                        disabled={busy}
                        slotProps={{ htmlInput: { min: 0, max: 30 } }}
                        fullWidth
                        hiddenLabel
                      />
                    </Box>
                    <Box sx={{ width: { xs: '100%', sm: 140 } }}>
                      <FieldLabel>Max years</FieldLabel>
                      <TextField
                        type="number"
                        value={maxYears}
                        onChange={(e) => setMaxYears(Number(e.target.value))}
                        disabled={busy}
                        slotProps={{ htmlInput: { min: 0, max: 30 } }}
                        fullWidth
                        hiddenLabel
                      />
                    </Box>
                  </Stack>
                  <Box>
                    <FieldLabel>Reject if title contains</FieldLabel>
                    <TextField
                      value={rejectTitles}
                      onChange={(e) => setRejectTitles(e.target.value)}
                      fullWidth
                      multiline
                      minRows={3}
                      disabled={busy}
                      helperText="One substring per line. Case-insensitive."
                      hiddenLabel
                    />
                  </Box>
                </Section>

                {errorMessage && (
                  <Alert severity="error" sx={{ borderRadius: 2 }}>
                    {errorMessage}
                  </Alert>
                )}

                {partialDownloadAvailable && currentRunId && (
                  <Alert
                    severity="warning"
                    sx={{ borderRadius: 2 }}
                    action={
                      <Button
                        color="inherit"
                        size="small"
                        href={`/api/runs/${currentRunId}/report.html`}
                        download="job-hunter-partial-report.html"
                        sx={{ fontWeight: 600, whiteSpace: 'nowrap' }}
                      >
                        Download partial results
                      </Button>
                    }
                  >
                    Sources finished before the run failed — partial results are available.
                  </Alert>
                )}

                <Button
                  type="submit"
                  variant="contained"
                  color="primary"
                  size="large"
                  fullWidth
                  disabled={busy}
                  startIcon={submitting ? <CircularProgress size={18} color="inherit" /> : null}
                >
                  {submitting ? 'Starting…' : 'Run now'}
                </Button>
                <Typography variant="caption" color="text.secondary" sx={{ textAlign: 'center', display: 'block', mt: -1 }}>
                  Often several minutes (large job boards + LLM scoring can take 15+ minutes).
                  Keep this tab open to download the HTML report. A copy is saved in this
                  browser after a successful run so you can download again without
                  re-running.
                </Typography>
              </Stack>
            </Box>
          </>
        )}
      </Container>

      <Box
        component="footer"
        sx={{
          px: { xs: 2, sm: 3 },
          py: 2,
          mt: 'auto',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          borderTop: '1px solid',
          borderColor: 'divider',
          bgcolor: 'background.paper',
        }}
      >
        <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
          <Box
            sx={{
              width: 8,
              height: 8,
              borderRadius: '50%',
              bgcolor: busy ? 'warning.main' : 'success.main',
              flexShrink: 0,
            }}
          />
          <Typography
            variant="caption"
            sx={{
              fontWeight: 600,
              letterSpacing: '0.12em',
              color: 'text.secondary',
              fontSize: '0.65rem',
            }}
          >
            {busy ? 'RUN IN PROGRESS' : 'AUTOMATION ENGINE READY'}
          </Typography>
        </Stack>
        <Typography variant="caption" color="text.secondary" sx={{ fontSize: '0.7rem' }}>
          v{APP_VERSION}
        </Typography>
      </Box>
    </Box>
  )
}

function LastRunCacheCard({
  meta,
  hasReportHtml,
  onDownloadCached,
  onForget,
}: {
  meta: LastRunCacheMeta
  hasReportHtml: boolean
  onDownloadCached: () => void
  onForget: () => void
}) {
  const when = useMemo(
    () =>
      new Date(meta.savedAt).toLocaleString(undefined, {
        dateStyle: 'medium',
        timeStyle: 'short',
      }),
    [meta.savedAt],
  )
  const sent = meta.summary?.sent ?? 0
  return (
    <Paper
      elevation={0}
      sx={{
        mb: 3,
        p: 2,
        borderRadius: 2,
        border: '1px solid',
        borderColor: 'divider',
        bgcolor: 'action.hover',
      }}
    >
      <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 0.5 }}>
        Last completed run
      </Typography>
      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1 }}>
        {when}
        {meta.email ? ` · ${meta.email}` : ''}
      </Typography>
      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1.5 }}>
        Shown only after the server finishes the full pipeline and sends success. Runs that
        stop midway, return an error (including late failures), or lose the connection do
        not replace this. You can always start a new run with <strong>Run now</strong>{' '}
        below; failed attempts leave this snapshot unchanged.
      </Typography>
      <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap', mb: 2, gap: 0.5 }}>
        <Chip label={`${meta.summary.sources ?? 0} sources`} size="small" />
        <Chip label={`${meta.summary.window ?? 0} in 24h`} size="small" />
        <Chip label={`${meta.summary.rules ?? 0} pass rules`} size="small" />
        <Chip
          label={`${sent} matched`}
          size="small"
          color={sent > 0 ? 'success' : 'default'}
        />
      </Stack>
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1} sx={{ alignItems: 'stretch' }}>
        {hasReportHtml ? (
          <Button
            variant="contained"
            color="primary"
            size="small"
            onClick={onDownloadCached}
            sx={{ borderRadius: 2, fontWeight: 600 }}
          >
            Download saved HTML report
          </Button>
        ) : (
          <Alert severity="info" sx={{ py: 0.5, flex: 1 }}>
            No offline HTML for this saved completion (e.g. email-only run, storage quota,
            or download not available). Use <strong>Run now</strong> to run again.
          </Alert>
        )}
        <Button variant="text" size="small" color="inherit" onClick={onForget}>
          Forget saved run
        </Button>
      </Stack>
    </Paper>
  )
}

function TierBanner({
  hasByok,
  onOpenByok,
  gateInfo,
}: {
  hasByok: boolean
  onOpenByok: () => void
  gateInfo?: { ip_count: number; ip_cap: number; global_count: number; global_cap: number } | null
}) {
  if (hasByok) {
    return (
      <Alert severity="success" variant="outlined" sx={{ borderRadius: 2, bgcolor: 'rgba(46, 125, 50, 0.06)' }}>
        Using your own API keys — all sources enabled, no daily caps.
      </Alert>
    )
  }
  const quotaLine = gateInfo
    ? ` · Run ${gateInfo.ip_count}/${gateInfo.ip_cap} for you today, ${gateInfo.global_count}/${gateInfo.global_cap} globally.`
    : ''
  return (
    <Alert
      severity="info"
      variant="outlined"
      sx={{ borderRadius: 2, bgcolor: 'rgba(47, 79, 216, 0.06)' }}
      action={
        <Button color="inherit" size="small" onClick={onOpenByok} sx={{ fontWeight: 600 }}>
          Add keys
        </Button>
      }
    >
      Free tier: 1 run/day per IP · Greenhouse, Lever &amp; remote RSS only
      (LinkedIn/Naukri need your own keys). Add a free Groq key to unlock all
      sources and skip the cap.{quotaLine}
    </Alert>
  )
}

function ByokSection({
  open,
  onToggle,
  byok,
  onChange,
  onClear,
  disabled,
}: {
  open: boolean
  onToggle: () => void
  byok: Byok
  onChange: (next: Byok) => void
  onClear: () => void
  disabled?: boolean
}) {
  return (
    <Paper
      variant="outlined"
      sx={{
        p: 2.5,
        borderRadius: 2,
        bgcolor: 'background.paper',
        boxShadow: '0 1px 3px rgba(0,0,0,0.04)',
      }}
    >
      <Stack
        direction="row"
        sx={{ alignItems: 'center', justifyContent: 'space-between' }}
      >
        <Typography variant="subtitle2" sx={{ fontWeight: 600, letterSpacing: '0.04em', textTransform: 'uppercase', fontSize: '0.72rem', color: 'text.secondary' }}>
          Your API keys (optional)
        </Typography>
        <Button size="small" onClick={onToggle} sx={{ fontWeight: 600 }}>
          {open ? 'Hide' : 'Show'}
        </Button>
      </Stack>
      <Collapse in={open}>
        <Stack spacing={1.5} sx={{ mt: 1.5 }}>
          <Typography variant="caption" color="text.secondary">
            Keys stay in your browser (localStorage) and are sent only with your
            run requests. Free signups:{' '}
            <Link href="https://console.groq.com" target="_blank" rel="noreferrer">
              Groq
            </Link>
            {' · '}
            <Link
              href="https://aistudio.google.com/apikey"
              target="_blank"
              rel="noreferrer"
            >
              Gemini
            </Link>
            {' · '}
            <Link href="https://openrouter.ai/keys" target="_blank" rel="noreferrer">
              OpenRouter
            </Link>
            {' · '}
            <Link href="https://serpapi.com/manage-api-key" target="_blank" rel="noreferrer">
              SerpAPI
            </Link>
          </Typography>
          <TextField
            label="Groq API key"
            value={byok.groq}
            onChange={(e) => onChange({ ...byok, groq: e.target.value })}
            type="password"
            size="small"
            fullWidth
            disabled={disabled}
          />
          <TextField
            label="Gemini API key"
            value={byok.gemini}
            onChange={(e) => onChange({ ...byok, gemini: e.target.value })}
            type="password"
            size="small"
            fullWidth
            disabled={disabled}
          />
          <TextField
            label="OpenRouter API key"
            value={byok.openrouter}
            onChange={(e) => onChange({ ...byok, openrouter: e.target.value })}
            type="password"
            size="small"
            fullWidth
            disabled={disabled}
          />
          <TextField
            label="SerpAPI key (unlocks Google Jobs source)"
            value={byok.serpapi}
            onChange={(e) => onChange({ ...byok, serpapi: e.target.value })}
            type="password"
            size="small"
            fullWidth
            disabled={disabled}
          />
          <Box>
            <Button size="small" onClick={onClear} disabled={disabled}>
              Clear all keys
            </Button>
          </Box>
        </Stack>
      </Collapse>
    </Paper>
  )
}

function Section({
  title,
  hint,
  children,
}: {
  title: string
  hint?: string
  children: ReactNode
}) {
  return (
    <Paper
      variant="outlined"
      sx={{
        p: { xs: 2, sm: 3 },
        borderRadius: 2,
        bgcolor: 'background.paper',
        boxShadow: '0 1px 3px rgba(0,0,0,0.04)',
      }}
    >
      <Typography
        variant="subtitle2"
        gutterBottom
        sx={{
          fontWeight: 600,
          letterSpacing: '0.04em',
          textTransform: 'uppercase',
          fontSize: '0.72rem',
          color: 'text.secondary',
          mb: hint ? 0.5 : 1.5,
        }}
      >
        {title}
      </Typography>
      {hint && (
        <Typography variant="caption" color="text.secondary" sx={{ mb: 2, display: 'block', lineHeight: 1.5 }}>
          {hint}
        </Typography>
      )}
      <Stack spacing={2.5}>{children}</Stack>
    </Paper>
  )
}

function DropZone({
  file,
  dragOver,
  onDragEnter,
  onDragLeave,
  onDrop,
  onClick,
  onClear,
  disabled,
}: {
  file: File | null
  dragOver: boolean
  onDragEnter: () => void
  onDragLeave: () => void
  onDrop: (e: React.DragEvent<HTMLDivElement>) => void
  onClick: () => void
  onClear: () => void
  disabled?: boolean
}) {
  return (
    <Box
      onClick={disabled ? undefined : onClick}
      onDragEnter={onDragEnter}
      onDragOver={(e) => {
        e.preventDefault()
        onDragEnter()
      }}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
      sx={{
        p: 3,
        border: '2px dashed',
        borderColor: dragOver ? 'primary.main' : 'rgba(26, 29, 38, 0.12)',
        bgcolor: dragOver ? 'rgba(47, 79, 216, 0.06)' : 'rgba(255,255,255,0.6)',
        borderRadius: 2,
        cursor: disabled ? 'not-allowed' : 'pointer',
        textAlign: 'center',
        transition: 'border-color 0.2s, background-color 0.2s',
      }}
    >
      {file ? (
        <Stack
          direction="row"
          spacing={1}
          sx={{ justifyContent: 'center', alignItems: 'center', flexWrap: 'wrap' }}
        >
          <Chip label={file.name} size="small" sx={{ fontWeight: 500 }} />
          <Typography variant="caption" color="text.secondary">
            {Math.round(file.size / 1024)} KB
          </Typography>
          <IconButton
            size="small"
            onClick={(e) => {
              e.stopPropagation()
              onClear()
            }}
            disabled={disabled}
            aria-label="remove resume"
          >
            ×
          </IconButton>
        </Stack>
      ) : (
        <Stack spacing={1.25} sx={{ alignItems: 'center' }}>
          <UploadDocIcon />
          <Typography variant="body1" sx={{ fontWeight: 600, color: 'text.primary' }}>
            Click to upload or drag and drop
          </Typography>
          <Typography variant="caption" color="text.secondary">
            Markdown, plain text, or PDF (text-based PDFs; scanned pages may not work) · max 256 KB
          </Typography>
        </Stack>
      )}
    </Box>
  )
}

function RunningCard({
  step,
  lastMessage,
  progress,
}: {
  step: number
  lastMessage: string
  progress: ProgressEvent[]
}) {
  const recent = progress.slice(-8)
  return (
    <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', boxShadow: '0 1px 3px rgba(0,0,0,0.06)' }}>
      <CardContent sx={{ p: { xs: 2, sm: 3 } }}>
        <Stepper activeStep={step} alternativeLabel sx={{ mb: 2 }}>
          {PHASES.map((label) => (
            <Step key={label}>
              <StepLabel>{label}</StepLabel>
            </Step>
          ))}
        </Stepper>
        <LinearProgress sx={{ mb: 2 }} />
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
          {lastMessage}
        </Typography>
        <Divider sx={{ my: 1 }} />
        <Box
          component="ul"
          sx={{
            m: 0,
            pl: 2,
            maxHeight: 200,
            overflow: 'auto',
            fontSize: 12,
            color: 'text.secondary',
          }}
        >
          {recent.map((ev, i) => (
            <li key={`${ev.ts}-${i}`}>
              {ev.message ?? `${ev.phase ?? ''} ${ev.source ?? ''}`.trim()}
            </li>
          ))}
        </Box>
      </CardContent>
    </Card>
  )
}

function DoneCard({
  summary,
  emailSent,
  downloadAvailable,
  runId,
  email,
  tier,
  onAgain,
  browserReportSaved,
}: {
  summary: RunSummary | null
  emailSent: boolean
  downloadAvailable: boolean
  runId: string | null
  email: string
  tier: 'owner' | 'byok' | 'free' | null
  onAgain: () => void
  browserReportSaved: boolean
}) {
  const sent = summary?.sent ?? 0
  const downloadHref =
    runId && downloadAvailable ? `/api/runs/${runId}/report.html` : undefined

  return (
    <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', boxShadow: '0 1px 3px rgba(0,0,0,0.06)' }}>
      <CardContent sx={{ p: { xs: 2, sm: 3 } }}>
        <Typography variant="h5" color="success.main" gutterBottom sx={{ fontWeight: 700 }}>
          Done
        </Typography>
        {emailSent ? (
          <Alert severity="success" sx={{ mb: 2 }}>
            Sent <strong>{sent}</strong> match{sent === 1 ? '' : 'es'} to{' '}
            <strong>{email}</strong>. Check your inbox (and spam).
          </Alert>
        ) : downloadAvailable && downloadHref ? (
          <Alert severity="success" sx={{ mb: 2 }}>
            Email was not sent (guest or SMTP not used for this address). Download
            the same HTML report the email would have contained:
          </Alert>
        ) : (
          <Alert severity="warning" sx={{ mb: 2 }}>
            Run completed but no email was sent and no download was available.
            Check server logs and SMTP / JOB_HUNTER_UI_OWNER_EMAIL.
          </Alert>
        )}

        {downloadAvailable && browserReportSaved && (
          <Alert severity="info" sx={{ mb: 2 }}>
            A copy of the HTML report is stored in this browser. From the start screen you
            can download it again without running the pipeline.
          </Alert>
        )}

        {downloadHref && (
          <Button
            variant="contained"
            href={downloadHref}
            download="job-hunter-report.html"
            sx={{ mb: 2, display: 'block' }}
          >
            Download HTML report
          </Button>
        )}

        {summary && (
          <Stack direction="row" spacing={1} sx={{ mb: 2, flexWrap: 'wrap' }}>
            {tier && (
              <Chip
                label={`tier: ${tier}`}
                size="small"
                color={tier === 'free' ? 'default' : 'primary'}
              />
            )}
            <Chip label={`${summary.sources ?? 0} sources`} size="small" />
            <Chip label={`${summary.window ?? 0} in last 24h`} size="small" />
            <Chip label={`${summary.rules ?? 0} pass rules`} size="small" />
            <Chip label={`${summary.llm ?? 0} scored`} size="small" />
            <Chip
              label={`${sent} matched`}
              size="small"
              color={sent > 0 ? 'success' : 'default'}
            />
            {(summary.new_greenhouse || summary.new_lever) ? (
              <Chip
                label={`+${(summary.new_greenhouse ?? 0) + (summary.new_lever ?? 0)} new companies`}
                size="small"
                color="primary"
              />
            ) : null}
          </Stack>
        )}

        <Button variant="outlined" onClick={onAgain} sx={{ borderRadius: 2, fontWeight: 600 }}>
          Run again
        </Button>
      </CardContent>
    </Card>
  )
}
