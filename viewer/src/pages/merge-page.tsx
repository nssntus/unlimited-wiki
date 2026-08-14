import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link, useNavigate, useSearchParams } from "react-router-dom"
import { MergeIcon, RotateCcwIcon } from "lucide-react"
import { toast } from "sonner"

import { apiGet, apiPost, type Article, type ArticleSummary, queryKeys } from "@/lib/api"
import { InlinePath } from "@/components/markdown-content"
import { PageFrame, PageTitle } from "@/components/page-frame"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger } from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"

type MergePreview = { source: Article; target: Article; merged: { aliases: string[]; sources: string[]; raw: string[] }; inbound_changes: { path: string; title: string }[]; redirect: { from: string; to: string }; recoverable: boolean }

export function MergePage() {
  const [params] = useSearchParams(); const navigate = useNavigate(); const client = useQueryClient()
  const [selectedSource, setSource] = useState(params.get("source") || ""); const [target, setTarget] = useState(params.get("target") || ""); const [operationId, setOperationId] = useState<string | null>(null)
  const articles = useQuery({ queryKey: queryKeys.articles, queryFn: () => apiGet<ArticleSummary[]>("/api/articles") })
  const source = selectedSource || articles.data?.[0]?.path || ""
  const preview = useQuery({ queryKey: ["merge-preview", source, target], queryFn: () => apiGet<MergePreview>(`/api/merge/preview?source=${encodeURIComponent(source)}&target=${encodeURIComponent(target)}`), enabled: Boolean(source && target && source !== target), retry: false })
  const commit = useMutation({ mutationFn: () => apiPost<{ operation_id: string; article: Article }>("/api/merge/commit", { source, target }), onSuccess: (result) => { setOperationId(result.operation_id); void client.invalidateQueries(); toast.success("正本已合并"); navigate(`/${result.article.path}`) }, onError: (error) => toast.error(error.message) })
  const rollback = useMutation({ mutationFn: () => apiPost(`/api/operations/${operationId}/rollback`, {}), onSuccess: () => { void client.invalidateQueries(); toast.success("合并已回滚"); setOperationId(null) }, onError: (error) => toast.error(error.message) })
  return <PageFrame><div className="mx-auto max-w-5xl"><PageTitle eyebrow="治理 / 唯一正本" title="合并正本" description="源词条退出索引并保留旧路径重定向；目标集合去重，所有入链在一个事务中更新。" />
    {operationId && <Alert className="mb-6"><RotateCcwIcon /><AlertTitle>合并可恢复</AlertTitle><AlertDescription className="flex flex-wrap items-center justify-between gap-3"><span>Operation <InlinePath>{operationId}</InlinePath></span><AlertDialog><AlertDialogTrigger render={<Button size="sm" variant="outline" />}>回滚</AlertDialogTrigger><AlertDialogContent><AlertDialogHeader><AlertDialogTitle>回滚本次合并？</AlertDialogTitle><AlertDialogDescription>仅当相关文件在合并后未继续修改时可回滚。</AlertDialogDescription></AlertDialogHeader><AlertDialogFooter><AlertDialogCancel>取消</AlertDialogCancel><AlertDialogAction disabled={rollback.isPending} onClick={() => rollback.mutate()}>确认回滚</AlertDialogAction></AlertDialogFooter></AlertDialogContent></AlertDialog></AlertDescription></Alert>}
    <FieldGroup className="mb-8 grid gap-4 sm:grid-cols-2"><Field><FieldLabel>源正本</FieldLabel><Select value={source} onValueChange={(value) => value && setSource(value)}><SelectTrigger className="w-full"><SelectValue placeholder="选择源词条" /></SelectTrigger><SelectContent><SelectGroup>{articles.data?.map((item) => <SelectItem key={item.path} value={item.path}>{item.title}</SelectItem>)}</SelectGroup></SelectContent></Select></Field><Field><FieldLabel>目标正本</FieldLabel><Select value={target} onValueChange={(value) => value && setTarget(value)}><SelectTrigger className="w-full"><SelectValue placeholder="选择保留正本" /></SelectTrigger><SelectContent><SelectGroup>{articles.data?.filter((item) => item.path !== source).map((item) => <SelectItem key={item.path} value={item.path}>{item.title}</SelectItem>)}</SelectGroup></SelectContent></Select></Field></FieldGroup>
    {!target ? <p className="py-20 text-center text-sm text-muted-foreground">选择目标正本后查看完整预览。</p> : preview.isLoading ? <Skeleton className="h-72 w-full" /> : preview.isError || !preview.data ? <Alert variant="destructive"><AlertTitle>不能合并</AlertTitle><AlertDescription>{preview.error?.message}</AlertDescription></Alert> : <><Tabs defaultValue="metadata"><TabsList><TabsTrigger value="metadata">集合预览</TabsTrigger><TabsTrigger value="links">入链变更</TabsTrigger><TabsTrigger value="history">历史与重定向</TabsTrigger></TabsList><TabsContent value="metadata" className="mt-6"><div className="grid gap-6 sm:grid-cols-3">{Object.entries(preview.data.merged).map(([key,values]) => <section key={key}><h3 className="text-sm font-medium capitalize">{key}</h3><ul className="mt-3 flex flex-col gap-2 text-sm text-muted-foreground">{values.length ? values.map((value) => <li key={value} className="break-all">{value}</li>) : <li>无</li>}</ul></section>)}</div></TabsContent><TabsContent value="links" className="mt-6"><div className="flex flex-col divide-y border-y">{preview.data.inbound_changes.length ? preview.data.inbound_changes.map((item) => <div key={item.path} className="flex items-center justify-between gap-4 py-3 text-sm"><span>{item.title}</span><InlinePath>{item.path}</InlinePath></div>) : <p className="py-6 text-sm text-muted-foreground">源词条暂无入链。</p>}</div></TabsContent><TabsContent value="history" className="mt-6"><Alert><AlertTitle>旧路径继续有效</AlertTitle><AlertDescription><InlinePath>{preview.data.redirect.from}</InlinePath> 将跳转到 <InlinePath>{preview.data.redirect.to}</InlinePath>，before-image 存在本地 `.wiki-state/history/`。</AlertDescription></Alert></TabsContent></Tabs><div className="mt-8 flex justify-end gap-2 border-t pt-4"><Button variant="outline" render={<Link to={`/${source}`} />}>取消</Button><Button disabled={commit.isPending} onClick={() => commit.mutate()}>{commit.isPending ? <Spinner data-icon="inline-start" /> : <MergeIcon data-icon="inline-start" />}确认合并</Button></div></>}
  </div></PageFrame>
}
