import { useEffect, useState } from 'react'
import { motion } from 'framer-motion'
import { Radar } from 'lucide-react'

const REDUCED_MOTION_QUERY = '(prefers-reduced-motion: reduce)'

function useReducedMotionPreference(): boolean {
  const [reduced, setReduced] = useState(
    () => typeof window !== 'undefined'
      && typeof window.matchMedia === 'function'
      && window.matchMedia(REDUCED_MOTION_QUERY).matches,
  )

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return
    const query = window.matchMedia(REDUCED_MOTION_QUERY)
    const update = () => setReduced(query.matches)
    query.addEventListener('change', update)
    return () => query.removeEventListener('change', update)
  }, [])

  return reduced
}

interface Props {
  actionRunning: boolean
  className?: string
}

/** A status glyph whose continuous pulse disappears when the OS requests less motion. */
export default function MonitorRadar({ actionRunning, className = '' }: Props) {
  const reducedMotion = useReducedMotionPreference()
  const pulse = actionRunning && !reducedMotion

  return (
    <motion.span
      className="inline-flex shrink-0"
      data-monitor-action-pulse={pulse}
      initial={false}
      animate={pulse ? { opacity: [1, 0.45, 1] } : { opacity: 1 }}
      transition={pulse ? { duration: 1.4, repeat: Infinity, ease: 'easeInOut' } : { duration: 0 }}
    >
      <Radar className={`lucide-inline ${className}`} aria-hidden />
    </motion.span>
  )
}
