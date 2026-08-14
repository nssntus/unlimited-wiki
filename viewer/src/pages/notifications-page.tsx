import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import { BellIcon, BookOpenIcon, CheckIcon, ExternalLinkIcon } from "lucide-react"
import { toast } from "sonner"

import { apiGet, apiPost, type ArticleSummary, type Notification, queryKeys } from "@/lib/api"
import { PageFrame, PageTitle } from "@/components/page-frame"
import { StatusBadge } from "@/components/markdown-content"
import { Button } from "@/components/ui/button"
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Skeleton } from "@/components/ui/skeleton"

export function NotificationsPage() {
  const client = useQueryClient()
  const notifications = useQuery({ queryKey: queryKeys.notifications, queryFn: () => apiGet<Notification[]>("/api/notifications"), refetchInterval: 15000 })
  const articles = useQuery({ queryKey: queryKeys.articles, queryFn: () => apiGet<ArticleSummary[]>("/api/articles") })
  const markRead = useMutation({ mutationFn: (id: string) => apiPost(`/api/notifications/${id}/read`, {}), onSuccess: async () => { await client.invalidateQueries({ queryKey: queryKeys.notifications }) }, onError: (error) => toast.error(error.message) })

  return <PageFrame><div className="mx-auto max-w-4xl"><PageTitle eyebrow="账号" title="通知" description="下架、重新上架和公开内容处理结果会保留在这里。" />
    {notifications.isLoading ? <Skeleton className="h-72" /> : notifications.data?.length ? <div className="divide-y border-y">{notifications.data.map((item) => {
      const article = articles.data?.find((candidate) => candidate.title === item.title.replace(/^《|》.*$/g, ""))
      return <article key={item.id} className="flex flex-col gap-3 py-5"><div className="flex flex-wrap items-start justify-between gap-3"><div><div className="flex flex-wrap items-center gap-2"><h2 className="font-medium">{item.title}</h2>{!item.read_at && <StatusBadge value="未读" kind="warn" />}</div><p className="mt-2 text-sm text-muted-foreground">{item.message}</p><p className="mt-2 text-xs text-muted-foreground">{new Date(item.created_at).toLocaleString()}</p></div><div className="flex flex-wrap gap-2">{article && <Button size="sm" variant="outline" render={<Link to={`/${article.path}`} />}><BookOpenIcon data-icon="inline-start" />查看私有正本</Button>}{item.kind === "public_relisted" && <Button size="sm" variant="outline" render={<Link to={`/square/${item.object_id}`} />}><ExternalLinkIcon data-icon="inline-start" />查看广场</Button>}{!item.read_at && <Button size="sm" onClick={() => markRead.mutate(item.id)}><CheckIcon data-icon="inline-start" />标为已读</Button>}</div></div></article>
    })}</div> : <Empty><EmptyHeader><EmptyMedia variant="icon"><BellIcon /></EmptyMedia><EmptyTitle>暂无通知</EmptyTitle><EmptyDescription>公开内容状态变化后会在这里通知你。</EmptyDescription></EmptyHeader></Empty>}
  </div></PageFrame>
}
