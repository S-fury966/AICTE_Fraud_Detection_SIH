import { KeyRound, Lock, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { ROLE_LABEL } from '../lib/format'
import { useIdentity } from '../lib/identity'
import { Button } from './ui'

const ROLE_GRANTS: Record<string, string> = {
  senior_reviewer: 'Reads the audit trail, verifies the hash chain and re-runs applications under the current rules.',
  admin: 'Everything a senior reviewer can do. Intended for the people who maintain the rules.',
}

/** Passcode sign-in for a role the backend will not accept from a header. */
export default function SignInDialog({ role, onClose }: { role: string; onClose: () => void }) {
  const { signIn } = useIdentity()
  const [actor, setActor] = useState(role === 'admin' ? 'admin.rao' : 'senior.verma')
  const [passcode, setPasscode] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const passcodeRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    passcodeRef.current?.focus()
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await signIn(actor.trim(), role, passcode)
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setPasscode('')
      passcodeRef.current?.focus()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-ink-900/40 px-4 pt-[14vh] backdrop-blur-sm" onClick={onClose}>
      <form
        onSubmit={submit}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="sign-in-title"
        className="animate-rise w-full max-w-md overflow-hidden rounded-[1.75rem] bg-white shadow-2xl ring-1 ring-black/10"
      >
        <div className="relative overflow-hidden bg-ink-900 px-6 py-5 text-white">
          <svg className="pointer-events-none absolute inset-0 h-full w-full" viewBox="0 0 400 120" preserveAspectRatio="xMidYMid slice" aria-hidden>
            <circle cx="380" cy="130" r="90" fill="none" stroke="white" strokeOpacity="0.18" />
          </svg>
          <div className="relative flex items-start justify-between gap-3">
            <div>
              <span className="inline-flex items-center gap-1.5 rounded-full bg-butter-500 px-2.5 py-1 text-[11px] font-medium text-ink-900">
                <Lock className="h-3 w-3" /> Restricted role
              </span>
              <h2 id="sign-in-title" className="font-display mt-3 text-2xl font-light">
                Sign in as {ROLE_LABEL[role] ?? role}
              </h2>
            </div>
            <button type="button" onClick={onClose} className="rounded-full p-1 text-white/60 hover:bg-white/10 hover:text-white" aria-label="Close">
              <X className="h-4 w-4" />
            </button>
          </div>
          <p className="relative mt-2 text-xs leading-relaxed text-white/70">{ROLE_GRANTS[role]}</p>
        </div>
        <div className="space-y-4 p-6">
          <label className="block">
            <span className="label-xs">Your name</span>
            <input value={actor} onChange={(e) => setActor(e.target.value)} className="focus-ring mt-1 w-full rounded-xl px-3 py-2 text-sm ring-1 ring-black/10" autoComplete="username" />
          </label>
          <label className="block">
            <span className="label-xs">Role passcode</span>
            <input
              ref={passcodeRef}
              type="password"
              value={passcode}
              onChange={(e) => setPasscode(e.target.value)}
              className="focus-ring mt-1 w-full rounded-xl px-3 py-2 text-sm ring-1 ring-black/10"
              autoComplete="current-password"
            />
          </label>
          {error && <div className="rounded-xl bg-red-50 px-3 py-2 text-xs text-red-800">{error}</div>}
          <Button type="submit" className="w-full" disabled={busy || !actor.trim() || !passcode}>
            <KeyRound className="h-4 w-4" /> {busy ? 'Checking…' : 'Sign in'}
          </Button>
          <p className="text-[11px] leading-relaxed text-slate-500">
            The sign-in, and any refused attempt, is recorded in the audit trail. The passcode itself is never stored or logged. Signing out returns you to the officer role.
          </p>
        </div>
      </form>
    </div>
  )
}
