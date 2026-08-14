import type { ReactNode } from "react"

import { cn } from "@/lib/utils"

export function PageFrame({ children, aside, className }: { children: ReactNode; aside?: ReactNode; className?: string }) {
  return (
    <div className={cn("grid min-h-[calc(100svh-3.5rem)] min-w-0 grid-cols-1 xl:min-h-svh xl:grid-cols-[minmax(0,1fr)_19rem]", className)}>
      <main className="min-w-0 px-4 py-6 sm:px-8 lg:px-12 xl:px-14">{children}</main>
      {aside && <aside className="hidden min-w-0 border-l bg-sidebar/50 xl:block">{aside}</aside>}
    </div>
  )
}

export function PageTitle({ eyebrow, title, description, actions }: { eyebrow?: ReactNode; title: ReactNode; description?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="mb-8 flex min-w-0 flex-col gap-4 border-b pb-6 sm:flex-row sm:items-start sm:justify-between">
      <div className="min-w-0">
        {eyebrow && <div className="mb-3 text-sm text-muted-foreground">{eyebrow}</div>}
        <h1 className="break-words text-3xl leading-tight font-semibold sm:text-4xl">{title}</h1>
        {description && <div className="mt-3 max-w-2xl text-sm leading-6 text-muted-foreground">{description}</div>}
      </div>
      {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
    </header>
  )
}
