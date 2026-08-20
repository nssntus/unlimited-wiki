import { createContext, useContext } from "react"

export type UnsavedChangesValue = {
  setDirty: (dirty: boolean) => void
  runAfterDiscard: (action: () => unknown | Promise<unknown>) => Promise<void>
  allowNavigation: () => void
}

export const UnsavedChangesContext = createContext<UnsavedChangesValue | null>(null)

export function useUnsavedChanges() {
  const value = useContext(UnsavedChangesContext)
  if (!value) throw new Error("UnsavedChangesProvider is missing")
  return value
}
