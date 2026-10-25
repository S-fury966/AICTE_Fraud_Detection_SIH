import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'
import { api, DEFAULT_IDENTITY, loadIdentity, saveIdentity, setRequestIdentity, setSessionRejectedHandler, type Identity } from './api'
import type { Meta } from './types'

interface IdentityContextValue {
  identity: Identity
  /** Rename the officer. A signed-in role's name comes from its token. */
  setOfficerName: (actor: string) => void
  signIn: (actor: string, role: string, passcode: string) => Promise<void>
  signOut: () => Promise<void>
  /** True when the current role came from a passcode sign-in. */
  signedIn: boolean
  meta: Meta | null
  can: (permission: string) => boolean
  /** Bumped whenever identity changes, so pages refetch under the new role. */
  version: number
}

const IdentityContext = createContext<IdentityContextValue | null>(null)
const OFFICER_KEY = 'authentiq.officer'

function lastOfficer(): Identity {
  try {
    const name = localStorage.getItem(OFFICER_KEY)
    if (name) return { actor: name, role: 'officer' }
  } catch {
    /* storage unavailable */
  }
  return DEFAULT_IDENTITY
}

export function IdentityProvider({ children }: { children: ReactNode }) {
  const [identity, setIdentityState] = useState<Identity>(loadIdentity)
  const [meta, setMeta] = useState<Meta | null>(null)
  const [version, setVersion] = useState(0)

  const apply = useCallback((next: Identity) => {
    saveIdentity(next)
    setRequestIdentity(next)
    setIdentityState(next)
    setVersion((v) => v + 1)
  }, [])

  useEffect(() => {
    setRequestIdentity(identity)
    api.meta().then(setMeta).catch(() => setMeta(null))
  }, [identity])

  // The backend refused the token (expired, or the API restarted): back to officer.
  useEffect(() => {
    setSessionRejectedHandler(() => apply(lastOfficer()))
    return () => setSessionRejectedHandler(null)
  }, [apply])

  const setOfficerName = useCallback(
    (actor: string) => {
      if (identity.token) return
      try {
        localStorage.setItem(OFFICER_KEY, actor)
      } catch {
        /* non-fatal */
      }
      apply({ actor, role: 'officer' })
    },
    [identity.token, apply],
  )

  const signIn = useCallback(
    async (actor: string, role: string, passcode: string) => {
      const session = await api.signIn(actor, role, passcode)
      apply({ actor: session.actor, role: session.role, token: session.token, expiresAt: session.expires_at })
    },
    [apply],
  )

  const signOut = useCallback(async () => {
    try {
      await api.signOut()
    } catch {
      /* the session may already be gone; signing out locally is what matters */
    }
    apply(lastOfficer())
  }, [apply])

  const can = useCallback(
    (permission: string) => {
      // Until the permissions arrive, assume nothing: the backend enforces
      // them anyway, and a control that vanishes is better than one that 403s.
      const allowed = meta?.permissions?.[permission]
      return allowed ? allowed.includes(identity.role) : false
    },
    [meta, identity.role],
  )

  return (
    <IdentityContext.Provider value={{ identity, setOfficerName, signIn, signOut, signedIn: !!identity.token, meta, can, version }}>
      {children}
    </IdentityContext.Provider>
  )
}

export function useIdentity() {
  const value = useContext(IdentityContext)
  if (!value) throw new Error('useIdentity outside IdentityProvider')
  return value
}

/** Fetch with loading/error state; refetches when the identity changes. */
export function useApi<T>(loader: () => Promise<T>, deps: unknown[] = []) {
  const { version } = useIdentity()
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [loading, setLoading] = useState(true)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    loader()
      .then((result) => {
        if (!cancelled) {
          setData(result)
          setError(null)
        }
      })
      .catch((err) => !cancelled && setError(err))
      .finally(() => !cancelled && setLoading(false))
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [version, tick, ...deps])

  return { data, error, loading, reload: () => setTick((t) => t + 1), setData }
}
