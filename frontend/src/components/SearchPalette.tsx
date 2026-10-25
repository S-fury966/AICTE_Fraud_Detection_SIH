import { Building2, FileText, Fingerprint, Search, UserRound, X } from 'lucide-react'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import { DOCUMENT_TYPE_LABEL } from '../lib/format'
import type { SearchResults } from '../lib/types'

interface Item {
  key: string
  icon: ReactNode
  title: string
  subtitle: string
  to: string | null
}

function toItems(results: SearchResults): { group: string; items: Item[] }[] {
  return [
    {
      group: 'Institutions',
      items: results.institutions.map((r) => ({ key: `i-${r.institution}`, icon: <Building2 className="h-4 w-4" />, title: r.institution, subtitle: `${r.applications} application(s)`, to: r.application_id ? `/applications/${r.application_id}` : null })),
    },
    {
      group: 'Certificate numbers',
      items: results.certificates.map((r) => ({ key: `c-${r.document_id}-${r.value}-${r.source}`, icon: <Fingerprint className="h-4 w-4" />, title: r.value, subtitle: `${r.source} · ${r.institution}`, to: `/applications/${r.application_id}?tab=certificate` })),
    },
    {
      group: 'Faculty',
      items: results.faculty.map((r) => ({ key: `f-${r.faculty_id}-${r.institution}-${r.name}`, icon: <UserRound className="h-4 w-4" />, title: `${r.name}${r.faculty_id ? ` · ${r.faculty_id}` : ''}`, subtitle: `${r.department ?? '—'} · declared by ${r.institution ?? '—'}`, to: r.application_id ? `/applications/${r.application_id}?tab=faculty` : null })),
    },
    {
      group: 'Documents',
      items: results.documents.map((r) => ({ key: `d-${r.document_id}`, icon: <FileText className="h-4 w-4" />, title: r.filename, subtitle: `${DOCUMENT_TYPE_LABEL[r.document_type] ?? r.document_type} · ${r.institution ?? '—'}`, to: r.application_id ? `/applications/${r.application_id}` : null })),
    },
  ].filter((g) => g.items.length)
}

export default function SearchPalette() {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [groups, setGroups] = useState<{ group: string; items: Item[] }[]>([])
  const [active, setActive] = useState(0)
  const [searching, setSearching] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const navigate = useNavigate()

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setOpen((o) => !o)
      }
      if (e.key === 'Escape') setOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  useEffect(() => {
    if (open) setTimeout(() => inputRef.current?.focus(), 10)
  }, [open])

  useEffect(() => {
    if (query.trim().length < 2) return
    const handle = setTimeout(() => {
      setSearching(true)
      api
        .search(query.trim())
        .then((r) => {
          setGroups(toItems(r.results))
          setActive(0)
        })
        .catch(() => setGroups([]))
        .finally(() => setSearching(false))
    }, 220)
    return () => clearTimeout(handle)
  }, [query])

  // Results for a too-short query are hidden rather than cleared in an effect.
  const shown = query.trim().length < 2 ? [] : groups
  const flat = shown.flatMap((g) => g.items)
  function go(item: Item | undefined) {
    if (!item?.to) return
    navigate(item.to)
    setOpen(false)
    setQuery('')
  }

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        title="Search certificate numbers, faculty IDs, institutions (⌘K)"
        className="focus-ring flex h-10 items-center gap-2 rounded-full bg-white/80 px-3 text-xs text-slate-500 ring-1 ring-black/5 hover:bg-white"
      >
        <Search className="h-4 w-4 text-slate-700" />
        <span className="hidden xl:inline">Search</span>
        <kbd className="hidden rounded-full bg-slate-100 px-1.5 py-0.5 font-sans text-[10px] text-slate-500 xl:inline">⌘K</kbd>
      </button>
      {open && (
        <div className="fixed inset-0 z-50 flex items-start justify-center bg-ink-900/40 px-4 pt-[12vh] backdrop-blur-sm" onClick={() => setOpen(false)}>
          <div className="w-full max-w-2xl overflow-hidden rounded-[1.75rem] bg-white shadow-2xl ring-1 ring-black/5 animate-rise" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center gap-3 border-b border-slate-200/70 px-4 py-3">
              <Search className="h-4 w-4 text-slate-400" />
              <input
                ref={inputRef}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'ArrowDown') {
                    e.preventDefault()
                    setActive((a) => Math.min(a + 1, flat.length - 1))
                  } else if (e.key === 'ArrowUp') {
                    e.preventDefault()
                    setActive((a) => Math.max(a - 1, 0))
                  } else if (e.key === 'Enter') go(flat[active])
                }}
                placeholder="Where else does this appear? e.g. NBTA/DEMO/2024/00622, FAC-DEMO-03001, Malabar"
                className="flex-1 bg-transparent text-sm outline-none placeholder:text-slate-400"
              />
              <button onClick={() => setOpen(false)} className="text-slate-400 hover:text-slate-700" aria-label="Close">
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="max-h-[55vh] overflow-y-auto py-2">
              {query.trim().length < 2 && <div className="px-4 py-6 text-center text-xs text-slate-400">Type at least two characters. Searches are logged by hash, not by the text typed.</div>}
              {query.trim().length >= 2 && !searching && flat.length === 0 && <div className="px-4 py-6 text-center text-xs text-slate-400">No match in stored submissions.</div>}
              {shown.map((group) => (
                <div key={group.group} className="mb-1">
                  <div className="label-xs px-4 py-1.5">{group.group}</div>
                  {group.items.map((item) => {
                    const index = flat.indexOf(item)
                    return (
                      <button
                        key={item.key}
                        onMouseEnter={() => setActive(index)}
                        onClick={() => go(item)}
                        className={`flex w-full items-center gap-3 px-4 py-2 text-left ${index === active ? 'bg-slate-100' : ''}`}
                      >
                        <span className="text-slate-400">{item.icon}</span>
                        <span className="min-w-0 flex-1">
                          <span className="block truncate text-sm text-slate-800">{item.title}</span>
                          <span className="block truncate text-[11px] text-slate-500">{item.subtitle}</span>
                        </span>
                      </button>
                    )
                  })}
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </>
  )
}
