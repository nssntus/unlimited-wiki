import { useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { Link, useLocation, useNavigate } from "react-router-dom"
import { Clock3Icon, CopyIcon, EllipsisIcon, ExternalLinkIcon, FilePenLineIcon, InfoIcon, MergeIcon, RefreshCwIcon, SendIcon, Settings2Icon } from "lucide-react"
import { toast } from "sonner"

import { apiGet, type Article, type ArticleSummary, queryKeys, type Task } from "@/lib/api"
import { GenerationDialog, type GenerationRequest } from "@/features/generation-dialog"
import { GovernanceDialog } from "@/features/governance-dialog"
import { MarkdownContent, StatusBadge } from "@/components/markdown-content"
import { markdownToc, scrollToHeading } from "@/lib/markdown-toc"
import { PageFrame, PageTitle } from "@/components/page-frame"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from "@/components/ui/alert-dialog"
import { Badge } from "@/components/ui/badge"
import { Breadcrumb, BreadcrumbItem, BreadcrumbLink, BreadcrumbList, BreadcrumbPage, BreadcrumbSeparator } from "@/components/ui/breadcrumb"
import { Button } from "@/components/ui/button"
import { DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Drawer, DrawerContent, DrawerDescription, DrawerHeader, DrawerTitle, DrawerTrigger } from "@/components/ui/drawer"
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Separator } from "@/components/ui/separator"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { useSession } from "@/features/session-context"

function pathFromLocation(pathname: string) {
  return decodeURIComponent(pathname.replace(/^\//, ""))
}

function stripMetadata(markdown: string) {
  return markdown.replace(/^#\s+.*\n/, "").replace(/^>\s*(Category|Status|Aliases|Sources|Raw|Updated|Archived|Generation|Evidence):.*\n/gim, "").replace(/^\s+/, "")
}

function taskPresentation(task: Task | null) {
  if (!task) return null
  if (task.status === "queued" || task.status === "running") return { label: "生成中", kind: "warn" as const }
  if (task.status === "failed") return { label: "生成失败", kind: "bad" as const }
  if (task.status === "cancelled") return { label: "生成已取消", kind: "neutral" as const }
  if (task.result?.conflict === true) return { label: "生成冲突", kind: "warn" as const }
  return { label: "生成完成", kind: "good" as const }
}

function PublicationUpdatePrompt({ article }: { article: Article }) {
  const [open, setOpen] = useState(true)
  const navigate = useNavigate()
  return (
    <AlertDialog open={open} onOpenChange={setOpen}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>将这次更新发布到广场？</AlertDialogTitle>
          <AlertDialogDescription>
            私有正文已不同于广场版本 {article.publication.public_version}。提交后会生成新的不可变快照，并重新经过 AI 预审和 Admin 审核；审核通过前，广场仍展示旧版本。
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>暂不更新</AlertDialogCancel>
          <AlertDialogAction onClick={() => navigate(`/share?article=${encodeURIComponent(article.path)}`)}>提交更新</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

function PublicationAction({ article }: { article: Article }) {
  const publication = article.publication
  if (publication.state === "published" && publication.public_entry_id) {
    return <Button variant="outline" size="sm" render={<Link to={`/square/${publication.public_entry_id}`} />}><ExternalLinkIcon data-icon="inline-start" />查看广场版本</Button>
  }
  if (publication.state === "update_available") {
    return <Button variant="outline" size="sm" render={<Link to={`/share?article=${encodeURIComponent(article.path)}`} />}><RefreshCwIcon data-icon="inline-start" />提交广场更新</Button>
  }
  if (publication.state === "relist_available") {
    return <Button variant="outline" size="sm" render={<Link to={`/share?article=${encodeURIComponent(article.path)}`} />}><RefreshCwIcon data-icon="inline-start" />申请重新上架</Button>
  }
  if (publication.state === "removed") {
    return <Button variant="outline" size="sm" render={<Link to={`/edit/${article.path}`} />}><FilePenLineIcon data-icon="inline-start" />修改后申请重新上架</Button>
  }
  if ((publication.state === "submitted" || publication.state === "update_pending" || publication.state === "relist_pending") && publication.submission_id) {
    return <Button variant="outline" size="sm" render={<Link to={`/submissions/${publication.submission_id}`} />}><Clock3Icon data-icon="inline-start" />查看审核进度</Button>
  }
  return <Button variant="outline" size="sm" render={<Link to={`/share?article=${encodeURIComponent(article.path)}`} />}><SendIcon data-icon="inline-start" />分享到广场</Button>
}

function MetadataPanel({ article }: { article: Article }) {
  const headings = markdownToc(article.markdown)
  return (
    <div className="sticky top-0 h-svh overflow-y-auto p-5">
      <Tabs defaultValue="info">
        <TabsList variant="line" className="w-full"><TabsTrigger value="info">信息</TabsTrigger><TabsTrigger value="toc">目录</TabsTrigger><TabsTrigger value="backlinks">反链</TabsTrigger></TabsList>
        <TabsContent value="info" className="pt-5">
          <dl className="flex flex-col gap-5 text-sm">
            <div><dt className="text-xs text-muted-foreground">分类</dt><dd className="mt-1 font-medium">{article.category_label}</dd></div>
            <div><dt className="text-xs text-muted-foreground">标签</dt><dd className="mt-1 flex flex-wrap gap-1">{article.tags.length ? article.tags.map((item) => <Badge key={item} variant="outline">{item}</Badge>) : <span className="text-muted-foreground">无</span>}</dd></div>
            <div><dt className="text-xs text-muted-foreground">正本别名</dt><dd className="mt-1 flex flex-wrap gap-1">{article.aliases.length ? article.aliases.map((item) => <Badge key={item} variant="outline">{item}</Badge>) : <span className="text-muted-foreground">无</span>}</dd></div>
            <Separator />
            <div><dt className="text-xs text-muted-foreground">Wiki Sources</dt><dd className="mt-2 flex flex-col gap-2 break-all">{article.sources.length ? article.sources.map((item) => <code key={item} className="text-xs">{item}</code>) : <span className="text-muted-foreground">未记录</span>}</dd></div>
            <div><dt className="text-xs text-muted-foreground">Raw 快照</dt><dd className="mt-2 flex flex-col gap-2 break-all">{article.raw.length ? article.raw.map((item) => <code key={item} className="text-xs">{item}</code>) : <span className="text-muted-foreground">未记录</span>}</dd></div>
            <Separator />
            <div><dt className="text-xs text-muted-foreground">Generation</dt><dd className="mt-1 break-words">{article.generation || "人工维护 / 未标记"}</dd></div>
            {article.remote_task?.error_type && <div><dt className="text-xs text-muted-foreground">远端失败</dt><dd className="mt-1 break-words text-destructive">{article.remote_task.error_type} · {article.remote_task.error_message}</dd></div>}
          </dl>
        </TabsContent>
        <TabsContent value="toc" className="pt-5"><nav className="flex flex-col gap-3 text-sm">{headings.map((heading) => <button key={heading.id} type="button" onClick={() => scrollToHeading(heading.id)} className="text-left text-muted-foreground hover:text-foreground" style={{ paddingInlineStart: `${(heading.depth - 2) * 12}px` }}>{heading.title}</button>)}</nav></TabsContent>
        <TabsContent value="backlinks" className="pt-5"><div className="flex flex-col gap-3">{article.backlinks.length ? article.backlinks.map((item) => <Link key={item.path} to={`/${item.path}`} className="text-sm text-link hover:underline">{item.title}</Link>) : <p className="text-sm text-muted-foreground">暂无反链</p>}</div></TabsContent>
      </Tabs>
    </div>
  )
}

export function ArticlePage() {
  const { hasPermission } = useSession()
  const canWrite = hasPermission("wiki.write")
  const location = useLocation()
  const navigate = useNavigate()
  const [generation, setGeneration] = useState<GenerationRequest | null>(null)
  const [govern, setGovern] = useState(false)
  const path = pathFromLocation(location.pathname)
  const articles = useQuery({ queryKey: queryKeys.articles, queryFn: () => apiGet<ArticleSummary[]>("/api/articles") })
  const defaultPath = articles.data?.[0]?.path
  const articlePath = path || defaultPath || ""
  const article = useQuery({
    queryKey: queryKeys.article(articlePath),
    queryFn: () => apiGet<Article>(`/api/article?path=${encodeURIComponent(articlePath)}`),
    enabled: Boolean(articlePath),
    refetchInterval: (query) => ["queued", "running"].includes(query.state.data?.remote_task?.status || "") ? 1500 : false,
  })
  const keywords = useQuery({ queryKey: ["keywords"], queryFn: () => apiGet<{ term: string; path: string | null; title: string; kind: "page" | "missing" }[]>("/api/keywords") })
  const content = useMemo(() => article.data ? stripMetadata(article.data.markdown) : "", [article.data])

  if (articles.isLoading || (articlePath && article.isLoading)) return <PageFrame><div className="mx-auto max-w-3xl"><Skeleton className="h-6 w-40" /><Skeleton className="mt-8 h-12 w-3/5" /><Skeleton className="mt-12 h-72 w-full" /></div></PageFrame>
  if (!articlePath) return <PageFrame><Empty className="min-h-[60svh]"><EmptyHeader><EmptyMedia variant="icon"><FilePenLineIcon /></EmptyMedia><EmptyTitle>知识库还是空的</EmptyTitle><EmptyDescription>将 Markdown 放入 Raw 后，从原料箱创建第一篇正本。</EmptyDescription></EmptyHeader><EmptyContent><Button render={<Link to="/inbox" />}>打开原料箱</Button></EmptyContent></Empty></PageFrame>
  if (article.isError || !article.data) return <PageFrame><Empty className="min-h-[60svh]"><EmptyHeader><EmptyMedia variant="icon"><FilePenLineIcon /></EmptyMedia><EmptyTitle>找不到这篇词条</EmptyTitle><EmptyDescription>{article.error?.message || articlePath}</EmptyDescription></EmptyHeader><EmptyContent><Button variant="outline" onClick={() => navigate("/")}>返回词库</Button></EmptyContent></Empty></PageFrame>
  const data = article.data
  const taskState = taskPresentation(data.remote_task)
  const badges = [
    <StatusBadge key="category" value={data.category_label} />,
    <StatusBadge key="classification" value={data.classification_status === "confirmed" ? "归类已确认" : data.classification_status === "sync_conflict" ? "分类冲突" : "待归类"} kind={data.classification_status === "confirmed" ? "good" : "warn"} />,
    <StatusBadge key="status" value={data.content_status} kind={data.content_status === "词条" ? "good" : data.content_status === "草稿" ? "warn" : "bad"} />,
    <StatusBadge key="complete" value={`结构 ${data.completeness}`} kind={data.completeness === "完整" ? "good" : "warn"} />,
    <StatusBadge key="evidence" value={data.evidence_status} kind={data.evidence_status.includes("待") ? "warn" : "neutral"} />,
    taskState ? <StatusBadge key="task" value={taskState.label} kind={taskState.kind} /> : null,
    data.publication.state === "published" ? <StatusBadge key="publication" value={`广场 v${data.publication.public_version}`} kind="good" /> : null,
    data.publication.state === "update_available" ? <StatusBadge key="publication" value="广场有旧版本" kind="warn" /> : null,
    data.publication.state === "update_pending" || data.publication.state === "submitted" ? <StatusBadge key="publication" value="广场审核中" kind="warn" /> : null,
    data.publication.state === "removed" ? <StatusBadge key="publication" value="广场已下架" kind="bad" /> : null,
    data.publication.state === "relist_available" ? <StatusBadge key="publication" value="可申请重新上架" kind="warn" /> : null,
    data.publication.state === "relist_pending" ? <StatusBadge key="publication" value="重新上架审核中" kind="warn" /> : null,
  ]
  return (
    <>
      <PageFrame aside={<MetadataPanel article={data} />}>
        <article className="mx-auto max-w-[760px]">
          {data.redirected_from && <Alert className="mb-6"><InfoIcon /><AlertTitle>已跳转到正本</AlertTitle><AlertDescription>旧路径 {data.redirected_from} 仍可访问，当前正本为 {data.path}。</AlertDescription></Alert>}
          {data.classification_status !== "confirmed" && <Alert className="mb-6"><InfoIcon /><AlertTitle>{data.classification_status === "sync_conflict" ? "分类与磁盘状态存在冲突" : "这篇词条等待归类确认"}</AlertTitle><AlertDescription>正文可以正常阅读和编辑，未经确认不会移动文件。<Button className="mt-3" size="sm" variant="outline" render={<Link to={`/classification?article=${data.article_id}`} />}>打开归类工作台</Button></AlertDescription></Alert>}
          {data.remote_task && ["queued", "running"].includes(data.remote_task.status) && <Alert className="mb-6"><InfoIcon /><AlertTitle>词条正在生成</AlertTitle><AlertDescription>当前先展示本地草稿；后台任务完成后，本页会自动刷新为生成结果。</AlertDescription></Alert>}
          {data.remote_task?.status === "failed" && <Alert variant="destructive" className="mb-6"><InfoIcon /><AlertTitle>词条生成失败</AlertTitle><AlertDescription>{data.remote_task.error_type || "model_error"} · {data.remote_task.error_message || "请在任务中心重试。"}</AlertDescription></Alert>}
          {data.remote_task?.result?.conflict === true && <Alert className="mb-6"><InfoIcon /><AlertTitle>生成结果未覆盖当前正文</AlertTitle><AlertDescription>生成期间正文发生变化，结果已被冲突保护拦截；请在任务中心基于当前正文重试。</AlertDescription></Alert>}
          {data.publication.state === "update_available" && <Alert className="mb-6"><RefreshCwIcon /><AlertTitle>广场版本需要更新</AlertTitle><AlertDescription className="flex flex-wrap items-center justify-between gap-3"><span>当前私有正文已修改，广场仍展示版本 {data.publication.public_version}。是否提交本次更新重新审核？</span><Button size="sm" render={<Link to={`/share?article=${encodeURIComponent(data.path)}`} />}>提交广场更新</Button></AlertDescription></Alert>}
          {data.publication.state === "removed" && <Alert variant="destructive" className="mb-6"><InfoIcon /><AlertTitle>该词条已从广场下架</AlertTitle><AlertDescription className="flex flex-wrap items-center justify-between gap-3"><span>{data.publication.moderation_reason || "请根据通知中的处理理由修改私有正本。原公开快照不会被改写。"}</span><Button variant="outline" size="sm" render={<Link to={`/edit/${data.path}`} />}>修改正文</Button></AlertDescription></Alert>}
          {data.publication.state === "relist_available" && <Alert className="mb-6"><RefreshCwIcon /><AlertTitle>修改完成后可申请重新上架</AlertTitle><AlertDescription className="flex flex-wrap items-center justify-between gap-3"><span>当前正文已不同于被下架版本。提交后会创建新快照，并重新经过 AI 预审和 Admin 审核。</span><Button size="sm" render={<Link to={`/share?article=${encodeURIComponent(data.path)}`} />}>申请重新上架</Button></AlertDescription></Alert>}
          {(data.publication.state === "submitted" || data.publication.state === "update_pending" || data.publication.state === "relist_pending") && <Alert className="mb-6"><Clock3Icon /><AlertTitle>{data.publication.state === "update_pending" ? "广场更新正在审核" : data.publication.state === "relist_pending" ? "重新上架申请正在审核" : "广场投稿正在审核"}</AlertTitle><AlertDescription className="flex flex-wrap items-center justify-between gap-3"><span>{data.publication.submission_matches_current ? "当前正文已固化为审核快照，审核通过前不会改变广场内容。" : "提交审核后正文又发生了变化；当前审核快照不会自动变化，审核结束后可再次提交。"}</span>{data.publication.submission_id && <Button variant="outline" size="sm" render={<Link to={`/submissions/${data.publication.submission_id}`} />}>查看进度</Button>}</AlertDescription></Alert>}
          <PageTitle
            eyebrow={<Breadcrumb><BreadcrumbList><BreadcrumbItem><BreadcrumbLink render={<Link to="/" />}>知识库</BreadcrumbLink></BreadcrumbItem><BreadcrumbSeparator /><BreadcrumbItem><BreadcrumbLink>{data.category_label}</BreadcrumbLink></BreadcrumbItem><BreadcrumbSeparator /><BreadcrumbItem><BreadcrumbPage>{data.title}</BreadcrumbPage></BreadcrumbItem></BreadcrumbList></Breadcrumb>}
            title={data.title}
            description={<div className="flex flex-wrap gap-1.5">{badges}</div>}
            actions={<>
              {canWrite && <>
              <Button variant="outline" size="sm" render={<Link to={`/edit/${data.path}`} />}><FilePenLineIcon data-icon="inline-start" />编辑</Button>
              <Button variant="outline" size="sm" onClick={() => setGovern(true)}><Settings2Icon data-icon="inline-start" />治理</Button>
              <PublicationAction article={data} />
              </>}
              <DropdownMenu><DropdownMenuTrigger render={<Button variant="ghost" size="icon-sm" aria-label="更多操作" />}><EllipsisIcon /></DropdownMenuTrigger><DropdownMenuContent align="end"><DropdownMenuGroup>
                <DropdownMenuItem onClick={() => { void navigator.clipboard.writeText(window.location.href); toast.success("深链已复制") }}><CopyIcon />复制深链</DropdownMenuItem>
                {canWrite && <DropdownMenuItem onClick={() => navigate(`/merge?source=${encodeURIComponent(data.path)}`)}><MergeIcon />合并正本</DropdownMenuItem>}
              </DropdownMenuGroup></DropdownMenuContent></DropdownMenu>
              <Drawer><DrawerTrigger render={<Button variant="ghost" size="icon-sm" className="xl:hidden" aria-label="查看页面信息" />}><InfoIcon /></DrawerTrigger><DrawerContent><DrawerHeader><DrawerTitle>词条信息</DrawerTitle><DrawerDescription>来源、生成路径与反链</DrawerDescription></DrawerHeader><div className="max-h-[70dvh] overflow-y-auto px-4 pb-6"><MetadataPanel article={data} /></div></DrawerContent></Drawer>
            </>}
          />
          <MarkdownContent markdown={content} fromPath={data.path} keywords={keywords.data} onMissingKeyword={canWrite ? (keyword) => setGeneration({ keyword, from_path: data.path }) : undefined} />
        </article>
      </PageFrame>
      {canWrite && <GenerationDialog request={generation} onOpenChange={(open) => !open && setGeneration(null)} />}
      {canWrite && <GovernanceDialog key={data.revision} article={data} open={govern} onOpenChange={setGovern} />}
      {canWrite && data.publication.state === "update_available" && <PublicationUpdatePrompt key={data.revision} article={data} />}
    </>
  )
}
