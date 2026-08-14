import { useEffect } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "react-router-dom"
import { BookOpenCheckIcon, Globe2Icon, SparklesIcon } from "lucide-react"
import { toast } from "sonner"

import { apiPost, queryKeys } from "@/lib/api"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { StatusBadge } from "@/components/markdown-content"

export type GenerationRequest = { keyword: string; from_path?: string; heading?: string; passage?: string }

type Preflight = {
  keyword: string
  existing_path: string | null
  category_label: string
  local_coverage: { sufficient: boolean; reason: string; evidence_count: number; document_count: number; char_count: number }
  context: { from_path: string; heading: string; passage: string }
  excerpts: { title: string; path: string; text: string }[]
  plan: string
}

export function GenerationDialog({ request, onOpenChange }: { request: GenerationRequest | null; onOpenChange: (open: boolean) => void }) {
  const navigate = useNavigate()
  const client = useQueryClient()
  const preflight = useQuery({
    queryKey: ["generate-preflight", request],
    queryFn: () => apiPost<Preflight>("/api/generate/preflight", request as Record<string, unknown>, false),
    enabled: Boolean(request),
  })
  const generate = useMutation({
    mutationFn: () => apiPost<{ article: { path: string }; task: { id: string } | null }>("/api/generate", request as Record<string, unknown>),
    onSuccess: (result) => {
      void client.invalidateQueries({ queryKey: queryKeys.articles })
      void client.invalidateQueries({ queryKey: queryKeys.tasks })
      toast.success(result.task ? "草稿已创建，补证任务已入队" : "本地词条已创建")
      onOpenChange(false)
      navigate(`/${result.article.path}`)
    },
    onError: (error) => toast.error(error.message),
  })

  useEffect(() => {
    if (preflight.data?.existing_path && request) {
      onOpenChange(false)
      navigate(`/${preflight.data.existing_path}`)
    }
  }, [navigate, onOpenChange, preflight.data, request])

  const data = preflight.data
  return (
    <Dialog open={Boolean(request)} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>生成「{request?.keyword}」</DialogTitle>
          <DialogDescription>确认本次义项、来源上下文和补证路径。</DialogDescription>
        </DialogHeader>
        {preflight.isLoading ? (
          <div className="flex flex-col gap-3"><Skeleton className="h-16 w-full" /><Skeleton className="h-28 w-full" /><Skeleton className="h-20 w-full" /></div>
        ) : preflight.isError ? (
          <Alert variant="destructive"><AlertTitle>无法检查本地资料</AlertTitle><AlertDescription>{preflight.error.message}</AlertDescription></Alert>
        ) : data ? (
          <div className="flex flex-col gap-5">
            <div className="grid gap-4 border-y py-4 sm:grid-cols-3">
              <div><div className="text-xs text-muted-foreground">分类建议</div><div className="mt-1 text-sm font-medium">{data.category_label}</div></div>
              <div><div className="text-xs text-muted-foreground">本地证据</div><div className="mt-1"><StatusBadge value={data.local_coverage.sufficient ? "充分" : "不足"} kind={data.local_coverage.sufficient ? "good" : "warn"} /></div></div>
              <div><div className="text-xs text-muted-foreground">证据范围</div><div className="mt-1 text-sm font-medium">{data.local_coverage.document_count} 篇 / {data.local_coverage.char_count} 字</div></div>
            </div>
            <Alert>
              {data.local_coverage.sufficient ? <BookOpenCheckIcon /> : <Globe2Icon />}
              <AlertTitle>{data.local_coverage.sufficient ? "直接使用本地资料" : "先建本地草稿，再后台补证"}</AlertTitle>
              <AlertDescription>{data.local_coverage.sufficient ? "该路径不会发起网页请求。" : "阅读不会等待网络；超时、证书、无结果和模型错误会分别记录。"}</AlertDescription>
            </Alert>
            <section>
              <h3 className="mb-2 text-sm font-medium">采用上下文</h3>
              <div className="border-l-2 pl-4 text-sm leading-6 text-muted-foreground">
                <div>{data.context.from_path || "当前页面"}{data.context.heading ? ` · ${data.context.heading}` : ""}</div>
                <p className="mt-1 text-foreground">{data.context.passage || "未提供段落，按本地资料中的主要义项处理。"}</p>
              </div>
            </section>
            <section>
              <h3 className="mb-2 text-sm font-medium">本地摘录</h3>
              <div className="flex max-h-56 flex-col gap-3 overflow-y-auto pr-2">
                {data.excerpts.length ? data.excerpts.map((excerpt) => (
                  <div key={`${excerpt.path}-${excerpt.text.slice(0, 20)}`} className="border-b pb-3 last:border-0">
                    <div className="text-xs font-medium">{excerpt.title} · {excerpt.path}</div>
                    <p className="mt-1 line-clamp-3 text-sm text-muted-foreground">{excerpt.text}</p>
                  </div>
                )) : <p className="text-sm text-muted-foreground">暂无直接摘录。</p>}
              </div>
            </section>
          </div>
        ) : null}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>取消</Button>
          <Button disabled={!data || generate.isPending} onClick={() => generate.mutate()}>
            {generate.isPending ? <Spinner data-icon="inline-start" /> : <SparklesIcon data-icon="inline-start" />}
            确认生成
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
