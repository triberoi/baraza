/**
 * Six routes, hand-rolled.
 *
 * A router library would be a dependency in a published package for a fixed set of
 * paths that will not grow — the tool has the screens it has. This is the History API
 * plus a `popstate` listener, and it is the whole thing.
 *
 * The server already serves `index.html` for any path (`baraza.web.ui`), so a reload
 * on `/people/mem_0001` reaches the app rather than a 404. That is what makes real
 * paths safe here instead of a hash.
 */
import { useCallback, useEffect, useState } from 'react'

export type Route =
  | { name: 'overview' }
  | { name: 'people' }
  | { name: 'person'; memberId: string }
  | { name: 'returning' }
  | { name: 'import' }
  | { name: 'settings' }

/**
 * Decode one path segment, or hand back what was there.
 *
 * `decodeURIComponent` throws `URIError` on a malformed escape — `%E0%A4%A`, a link
 * mangled by a chat client, a hand-typed address. This ran unguarded inside the
 * `useState` initializer, which is during the FIRST render, so it threw before anything
 * mounted and the organizer got a white page: no error, no nav, no way back. An id that
 * does not decode is an id nobody has, and the screen that says so beats nothing at all.
 */
function decodeSegment(raw: string): string {
  try {
    return decodeURIComponent(raw)
  } catch {
    return raw
  }
}

export function parse(pathname: string): Route {
  const parts = pathname.split('/').filter(Boolean)
  if (parts[0] === 'people' && parts[1]) return { name: 'person', memberId: decodeSegment(parts[1]) }
  if (parts[0] === 'people') return { name: 'people' }
  if (parts[0] === 'returning') return { name: 'returning' }
  if (parts[0] === 'import') return { name: 'import' }
  if (parts[0] === 'settings') return { name: 'settings' }
  return { name: 'overview' }
}

export function href(route: Route): string {
  switch (route.name) {
    case 'people':
      return '/people'
    case 'person':
      return `/people/${encodeURIComponent(route.memberId)}`
    case 'returning':
      return '/returning'
    case 'import':
      return '/import'
    case 'settings':
      return '/settings'
    default:
      return '/'
  }
}

export function useRoute(): [Route, (to: Route, options?: { replace?: boolean }) => void] {
  const [route, setRoute] = useState<Route>(() => parse(window.location.pathname))

  useEffect(() => {
    // The back button has to work. Without this the URL changes and the view does not,
    // which is the specific broken feeling that makes a local tool feel unfinished.
    const onPop = () => setRoute(parse(window.location.pathname))
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])

  // `replace` exists for a route the app CORRECTS rather than one the organizer chose.
  // Pushing there would put a URL nobody navigated to into the back stack, so the back
  // button would appear to do nothing.
  const go = useCallback((to: Route, options?: { replace?: boolean }) => {
    const write = options?.replace ? window.history.replaceState : window.history.pushState
    write.call(window.history, {}, '', href(to))
    setRoute(to)
  }, [])

  return [route, go]
}

/**
 * A real `<a>` with a real href, intercepted. Middle-click, ctrl-click and "copy link"
 * all keep working, which a `<button>` styled as a link would silently break.
 */
export function Link({
  to,
  children,
  className,
}: {
  to: Route
  children: React.ReactNode
  className?: string
}) {
  return (
    <a
      href={href(to)}
      className={className}
      onClick={(event) => {
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return
        event.preventDefault()
        window.history.pushState({}, '', href(to))
        window.dispatchEvent(new PopStateEvent('popstate'))
      }}
    >
      {children}
    </a>
  )
}
