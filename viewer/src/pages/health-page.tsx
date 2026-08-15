import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import { CheckCircle2Icon, MergeIcon, RefreshCwIcon, SparklesIcon } from "lucide-react"
import { toast } from "sonner"

import { apiGet, apiPost, type LintIssue, queryKeys } from "@/lib/api"
import { PageFrame, PageTitle } from "@/components/page-frame"
import { Button } from "@/components/ui/button"
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { useSession } from "@/features/session-context"

const kinds = [["dead_link","死链"],["missing_category","缺分类"],["missing_backlink","缺反链"],["near_duplicate","近似重复"],["content_quality","内容质量"]] as const

export function HealthPage() {
  const { hasPermission } = useSession()
  const canWrite = hasPermission("wiki.write")
  const client = useQueryClient()
  const [filter, setFilter] = useState<string[]>(kinds.map(([kind]) => kind))
  const lint = useQuery({ queryKey: queryKeys.lint, queryFn: () => apiGet<{ items: LintIssue[]; scanning: boolean }>("/api/lint"), refetchInterval: (query) => query.state.data?.scanning ? 1000 : false })
  const governance = useMutation({
    mutationFn: () => apiPost<{ queued: number }>("/api/governance", {}),
    onSuccess: ({ queued }) => {
      void client.invalidateQueries({ queryKey: queryKeys.tasks })
      toast.success(queued ? `已创建 ${queued} 个 AI 治理任务` : "没有需要 AI 治理的词条")
    },
    onError: (error) => toast.error(error.message),
  })
  const refresh = async () => {
    await apiGet<{ items: LintIssue[]; scanning: boolean }>("/api/lint?refresh=1")
    await lint.refetch()
  }
  const rows = lint.data?.items.filter((item) => filter.includes(item.kind)) ?? []
  return <PageFrame><div className="mx-auto max-w-5xl"><PageTitle eyebrow="诊断与治理" title="健康检查" description="扫描机器可判定的质量问题。AI 生成和治理结果都必须通过同一质量门禁；近似重复仍由你决定是否合并。" actions={<div className="flex flex-wrap gap-2">{canWrite && <Button size="sm" disabled={governance.isPending} onClick={() => governance.mutate()}>{governance.isPending ? <Spinner data-icon="inline-start" /> : <SparklesIcon data-icon="inline-start" />}AI 治理全部</Button>}<Button variant="outline" size="sm" onClick={() => void refresh()}><RefreshCwIcon data-icon="inline-start" />刷新</Button></div>} />
    <ToggleGroup value={filter} onValueChange={setFilter} multiple className="mb-6 flex flex-wrap justify-start">{kinds.map(([kind,label]) => <ToggleGroupItem key={kind} value={kind}>{label}</ToggleGroupItem>)}</ToggleGroup>
    {lint.isLoading ? <Skeleton className="h-64 w-full" /> : rows.length ? <div className="overflow-x-auto rounded-md border"><Table><TableHeader><TableRow><TableHead>类型</TableHead><TableHead>页面</TableHead><TableHead>明细</TableHead><TableHead className="text-right">定位</TableHead></TableRow></TableHeader><TableBody>{rows.map((issue) => <TableRow key={issue.id}><TableCell>{kinds.find(([kind]) => kind === issue.kind)?.[1]}</TableCell><TableCell>{issue.title}</TableCell><TableCell className="max-w-md break-words text-muted-foreground">{issue.detail}</TableCell><TableCell className="text-right">{canWrite && issue.kind === "near_duplicate" && issue.target ? <Button size="sm" variant="outline" render={<Link to={`/merge?source=${encodeURIComponent(issue.path)}&target=${encodeURIComponent(issue.target)}`} />}><MergeIcon data-icon="inline-start" />合并</Button> : <Button size="sm" variant="outline" render={<Link to={`/${issue.path}?issue=${encodeURIComponent(issue.id)}`} />}>打开</Button>}</TableCell></TableRow>)}</TableBody></Table></div> : <Empty className="min-h-72"><EmptyHeader><EmptyMedia variant="icon"><CheckCircle2Icon /></EmptyMedia><EmptyTitle>当前筛选下没有问题</EmptyTitle><EmptyDescription>{lint.data?.scanning ? "扫描仍在后台运行。" : "Wiki 结构检查已通过。"}</EmptyDescription></EmptyHeader></Empty>}
  </div></PageFrame>
}
