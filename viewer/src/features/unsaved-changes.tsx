import { useCallback, useEffect, useRef, useState, type ReactNode } from "react"
import { useBlocker } from "react-router-dom"

import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { UnsavedChangesContext } from "@/features/unsaved-changes-context"

export function UnsavedChangesProvider({ children }: { children: ReactNode }) {
  const dirtyRef = useRef(false)
  const bypassRef = useRef(false)
  const pendingRef = useRef<(() => unknown | Promise<unknown>) | null>(null)
  const resolveRef = useRef<(() => void) | null>(null)
  const [dirty, setDirtyState] = useState(false)
  const [open, setOpen] = useState(false)
  const blocker = useBlocker(({ currentLocation, nextLocation }) => (
    dirtyRef.current
    && !bypassRef.current
    && `${currentLocation.pathname}${currentLocation.search}${currentLocation.hash}`
      !== `${nextLocation.pathname}${nextLocation.search}${nextLocation.hash}`
  ))

  const setDirty = useCallback((next: boolean) => {
    dirtyRef.current = next
    setDirtyState(next)
  }, [])

  const allowNavigation = useCallback(() => {
    bypassRef.current = true
    setDirty(false)
  }, [setDirty])

  const runAfterDiscard = useCallback((action: () => unknown | Promise<unknown>) => {
    if (!dirtyRef.current) return Promise.resolve(action()).then(() => undefined)
    return new Promise<void>((resolve) => {
      pendingRef.current = action
      resolveRef.current = resolve
      setOpen(true)
    })
  }, [])

  useEffect(() => {
    if (blocker.state === "unblocked" && !pendingRef.current) bypassRef.current = false
  }, [blocker.state])

  const cancel = () => {
    if (blocker.state === "blocked") blocker.reset()
    pendingRef.current = null
    resolveRef.current?.()
    resolveRef.current = null
    setOpen(false)
  }

  const confirm = async () => {
    const action = pendingRef.current
    pendingRef.current = null
    bypassRef.current = true
    setOpen(false)
    if (blocker.state === "blocked") {
      blocker.proceed()
    } else if (action) {
      try {
        await action()
      } catch {
        // Mutations surface their own error state; keep the draft guard active.
      } finally {
        bypassRef.current = false
      }
    }
    resolveRef.current?.()
    resolveRef.current = null
  }

  return (
    <UnsavedChangesContext.Provider value={{ setDirty, runAfterDiscard, allowNavigation }}>
      {children}
      <AlertDialog open={(open || blocker.state === "blocked") && dirty} onOpenChange={(next) => { if (!next) cancel() }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>放弃未保存的词条？</AlertDialogTitle>
            <AlertDialogDescription>当前标题、正文和分类选择将不会保留。</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={cancel}>继续编辑</AlertDialogCancel>
            <AlertDialogAction onClick={() => void confirm()}>放弃并离开</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </UnsavedChangesContext.Provider>
  )
}
