import { createContext, useContext } from "react"

import type { Session } from "@/lib/api"

export type SessionValue = {
  session: Session | undefined
  loading: boolean
  signOut: () => Promise<void>
  switchWorkspace: (workspaceId: string) => Promise<void>
  switchingWorkspace: boolean
  hasPermission: (permission: string) => boolean
}
export const SessionContext = createContext<SessionValue | null>(null)

export function useSession() {
  const value = useContext(SessionContext)
  if (!value) throw new Error("SessionProvider is missing")
  return value
}
