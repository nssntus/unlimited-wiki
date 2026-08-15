import { useQuery } from "@tanstack/react-query"
import { Link, useLocation } from "react-router-dom"
import { InboxIcon } from "lucide-react"

import { apiGet, queryKeys } from "@/lib/api"
import { MarkdownContent, StatusBadge } from "@/components/markdown-content"
import { PageFrame, PageTitle } from "@/components/page-frame"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { useSession } from "@/features/session-context"

type RawDocument = { path: string; title: string; markdown: string; revision: string }

export function RawPage() {
  const { hasPermission } = useSession()
  const canWrite = hasPermission("wiki.write")
  const path = decodeURIComponent(useLocation().pathname.replace(/^\/raw\//, "raw/"))
  const raw = useQuery({ queryKey: queryKeys.raw(path), queryFn: () => apiGet<RawDocument>(`/api/raw?path=${encodeURIComponent(path)}`) })
  if (raw.isLoading) return <PageFrame><div className="mx-auto max-w-[760px]"><Skeleton className="h-10 w-1/2" /><Skeleton className="mt-10 h-80 w-full" /></div></PageFrame>
  if (raw.isError || !raw.data) return <PageFrame><div className="mx-auto max-w-[760px] text-sm text-destructive">{raw.error?.message || "Raw 不存在"}</div></PageFrame>
  return <PageFrame><article className="mx-auto max-w-[760px]"><PageTitle eyebrow={<StatusBadge value="Raw 只读快照" kind="warn" />} title={raw.data.title} description={<code className="break-all">{raw.data.path}</code>} actions={canWrite ? <Button render={<Link to={`/ingest/${raw.data.path}`} />}><InboxIcon data-icon="inline-start" />摄入</Button> : undefined} /><MarkdownContent markdown={raw.data.markdown.replace(/^#\s+.*\n/, "")} fromPath={raw.data.path} /></article></PageFrame>
}
