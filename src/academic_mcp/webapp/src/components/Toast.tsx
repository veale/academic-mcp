import { createContext, useContext, useState, useCallback, useRef } from 'react'
import type { ReactNode } from 'react'

type ToastLevel = 'success' | 'error' | 'info'

interface Toast {
  id: number
  message: string
  level: ToastLevel
}

interface ToastContextValue {
  show: (message: string, level?: ToastLevel) => void
}

const ToastContext = createContext<ToastContextValue>({ show: () => {} })

export function useToast() {
  return useContext(ToastContext)
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const nextId = useRef(0)

  const show = useCallback((message: string, level: ToastLevel = 'info') => {
    const id = nextId.current++
    setToasts((prev) => [...prev, { id, message, level }])
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id))
    }, 3500)
  }, [])

  const bg: Record<ToastLevel, string> = {
    success: 'bg-green-600',
    error: 'bg-red-600',
    info: 'bg-gray-700',
  }

  return (
    <ToastContext.Provider value={{ show }}>
      {children}
      <div className="fixed bottom-4 right-4 flex flex-col gap-2 z-50 pointer-events-none">
        {toasts.map((t) => (
          <div
            key={t.id}
            className={[
              'px-4 py-2.5 rounded-lg text-white text-sm shadow-lg',
              'animate-in fade-in slide-in-from-bottom-2 duration-200',
              bg[t.level],
            ].join(' ')}
          >
            {t.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}
