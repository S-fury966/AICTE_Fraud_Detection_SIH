import { BookOpen, Info, Lock, LogOut, ShieldCheck, UserRound } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, NavLink, Outlet } from 'react-router-dom'
import { api } from '../lib/api'
import { useIdentity } from '../lib/identity'
import SearchPalette from './SearchPalette'
import { ROLE_LABEL } from '../lib/format'
import SignInDialog from './SignInDialog'
import { cn } from './ui'

const NAV = [
  { to: '/', label: 'Queue', end: true },
  { to: '/findings', label: 'Findings' },
  { to: '/submit', label: 'Scrutinise' },
  { to: '/connections', label: 'Connections' },
  { to: '/evaluation', label: 'Evaluation' },
  { to: '/audit', label: 'Audit' },
  { to: '/settings', label: 'Rules' },
]

export default function Layout() {
  const { identity, setOfficerName, signOut, signedIn, meta } = useIdentity()
  const [chainOk, setChainOk] = useState<boolean | null>(null)
  const [signingInAs, setSigningInAs] = useState<string | null>(null)
  const restricted = new Set(meta?.sign_in_required ?? ['senior_reviewer', 'admin'])

  function chooseRole(role: string) {
    if (role === identity.role) return
    if (restricted.has(role)) setSigningInAs(role)
    else if (signedIn) signOut()
  }

  useEffect(() => {
    api.health().then((h) => setChainOk(h.audit_chain_ok)).catch(() => setChainOk(null))
  }, [])

  return (
    <div className="mx-auto flex min-h-screen w-full max-w-[1480px] flex-col px-4 pb-8 sm:px-6">
      <header className="sticky top-0 z-30 -mx-4 mb-6 bg-gradient-to-b from-[#eeeeec] via-[#eeeeec]/90 to-transparent px-4 pt-4 pb-3 sm:-mx-6 sm:px-6">
        <div className="flex flex-wrap items-center gap-3">
          <Link to="/about" className="focus-ring flex items-center gap-2 rounded-full px-4 py-2 ring-1 ring-black/15 transition hover:bg-white/60">
            <ShieldCheck className="h-5 w-5 text-iris-600" />
            <span className="font-display text-xl font-medium tracking-tight text-slate-900">Authentiq</span>
          </Link>

          <nav className="order-3 flex w-full gap-1 overflow-x-auto rounded-full bg-white/70 p-1 ring-1 ring-black/5 backdrop-blur lg:order-none lg:ml-auto lg:w-auto">
            {NAV.map(({ to, label, end }) => (
              <NavLink
                key={to}
                to={to}
                end={end}
                className={({ isActive }) =>
                  cn('whitespace-nowrap rounded-full px-4 py-2 text-sm transition', isActive ? 'bg-ink-900 text-white shadow' : 'text-slate-600 hover:bg-black/5 hover:text-slate-900')
                }
              >
                {label}
              </NavLink>
            ))}
          </nav>

          <div className="ml-auto flex min-w-0 items-center gap-2 lg:ml-0">
            <SearchPalette />
            <NavLink to="/about" title="How it works" className="focus-ring hidden h-10 w-10 sm:flex items-center justify-center rounded-full bg-white/80 text-slate-700 ring-1 ring-black/5 hover:bg-white">
              <BookOpen className="h-4 w-4" />
            </NavLink>
            <div className="flex items-center gap-1 rounded-full bg-white/80 py-1 pl-1 pr-1 ring-1 ring-black/5">
              <span className={cn('flex h-8 w-8 items-center justify-center rounded-full', signedIn ? 'bg-ink-900 text-butter-500' : 'bg-butter-500 text-ink-900')}>
                {signedIn ? <Lock className="h-3.5 w-3.5" /> : <UserRound className="h-4 w-4" />}
              </span>
              {signedIn ? (
                <span className="max-w-24 truncate px-2 text-xs font-medium text-slate-800 sm:max-w-none" title="Signed in with the role passcode; the name comes from the sign-in">
                  {identity.actor}
                </span>
              ) : (
                <OfficerName key={identity.actor} actor={identity.actor} onCommit={setOfficerName} />
              )}
              <select
                className="focus-ring rounded-full bg-slate-100 px-2.5 py-1.5 text-xs font-medium text-slate-800"
                value={identity.role}
                onChange={(e) => chooseRole(e.target.value)}
                aria-label="Role"
              >
                {(meta?.roles ?? ['officer', 'senior_reviewer', 'admin']).map((role) => (
                  <option key={role} value={role}>
                    {ROLE_LABEL[role] ?? role}
                    {restricted.has(role) && role !== identity.role ? ' · sign in' : ''}
                  </option>
                ))}
              </select>
              {signedIn && (
                <button onClick={() => signOut()} className="focus-ring flex h-8 w-8 items-center justify-center rounded-full text-slate-500 hover:bg-slate-100 hover:text-slate-900" title="Sign out (back to officer)" aria-label="Sign out">
                  <LogOut className="h-3.5 w-3.5" />
                </button>
              )}
            </div>
          </div>
        </div>
      </header>

      {signingInAs && <SignInDialog role={signingInAs} onClose={() => setSigningInAs(null)} />}

      <main className="flex-1">
        <Outlet />
      </main>

      <footer className="mt-10 flex flex-wrap items-center justify-between gap-3 rounded-full bg-white/60 px-5 py-3 text-[11px] text-slate-500 ring-1 ring-black/5">
        <span className="flex items-center gap-2">
          <Info className="h-3.5 w-3.5 text-iris-600" />
          Decision support only — preliminary indicators with evidence; every decision rests with an official. Synthetic demo data; no real AICTE or issuer system is contacted.
        </span>
        <span className="flex items-center gap-3">
          <span>
            Field reader <span className="font-medium text-slate-700">{meta?.extraction_provider.active ?? '…'}</span>
          </span>
          <span className="flex items-center gap-1.5">
            <span className={cn('h-2 w-2 rounded-full', chainOk === null ? 'bg-slate-400' : chainOk ? 'bg-emerald-500' : 'bg-red-500')} />
            Audit chain {chainOk === null ? 'unknown' : chainOk ? 'intact' : 'BROKEN'}
          </span>
        </span>
      </footer>
    </div>
  )
}

/** Commits on blur or Enter: renaming on every keystroke would refetch every page. */
function OfficerName({ actor, onCommit }: { actor: string; onCommit: (name: string) => void }) {
  const [name, setName] = useState(actor)
  return (
    <input
      className="focus-ring w-20 rounded-full bg-transparent px-2 py-1 text-xs text-slate-800 sm:w-28"
      value={name}
      onChange={(e) => setName(e.target.value)}
      onBlur={() => (name.trim() && name.trim() !== actor ? onCommit(name.trim()) : setName(actor))}
      onKeyDown={(e) => e.key === 'Enter' && (e.target as HTMLInputElement).blur()}
      aria-label="Officer name"
    />
  )
}
