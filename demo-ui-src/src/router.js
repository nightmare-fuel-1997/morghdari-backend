// Hash router — zero dependencies. Routes:
//   ''                        -> farms (boss landing)
//   farms                     -> farms (boss landing)
//   overview                  -> engineering dashboard (v0.4 console)
//   saloon/:id                -> saloon page
//   saloon/:id/sensor/:slotId -> sensor detail
//   emqx                      -> EMQX page
import { useEffect, useState } from 'react'

export function useHashRoute() {
  const [hash, setHash] = useState(window.location.hash)
  useEffect(() => {
    const onChange = () => setHash(window.location.hash)
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [])
  const path = hash.replace(/^#\/?/, '')
  const parts = path.split('/').filter(Boolean)
  if (parts[0] === 'saloon' && parts[1] && parts[2] === 'sensor' && parts[3]) {
    return { page: 'sensor', saloonId: Number(parts[1]), slotId: decodeURIComponent(parts[3]) }
  }
  if (parts[0] === 'saloon' && parts[1]) {
    return { page: 'saloon', saloonId: Number(parts[1]) }
  }
  if (parts[0] === 'emqx') return { page: 'emqx' }
  if (parts[0] === 'overview') return { page: 'overview' }
  return { page: 'farms' }
}

export function navigate(to) {
  window.location.hash = to
}
