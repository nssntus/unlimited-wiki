import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useLocation, useNavigate } from "react-router-dom"
import { FileCheck2Icon } from "lucide-react"
import { toast } from "sonner"

import { apiGet, apiPost, type ArticleSummary, type Category, queryKeys, type RawInboxItem } from "@/lib/api"
import { MarkdownContent, StatusBadge } from "@/components/markdown-content"
import { PageFrame, PageTitle } from "@/components/page-frame"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Field, FieldDescription, FieldGroup, FieldLabel, FieldSet, FieldLegend } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"

type Preview = { raw: RawInboxItem; markdown: string; suggested_title: string; suggested_category: string; suggested_disposition: string; suggested_target: string | null; preview_changes: string[] }
type CommitResult = { target_path: string | null; article: { path: string } | null; task?: { id: string } | null }

export function IngestPage() {
  const path = decodeURIComponent(useLocation().pathname.replace(/^\/ingest\//, ""))
  const navigate = useNavigate(); const client = useQueryClient()
  const preview = useQuery({ queryKey: ["ingest-preview", path], queryFn: () => apiGet<Preview>(`/api/ingest/preview?path=${encodeURIComponent(path)}`) })
  const categories = useQuery({ queryKey: queryKeys.categories, queryFn: () => apiGet<Category[]>("/api/categories") })
  const articles = useQuery({ queryKey: queryKeys.articles, queryFn: () => apiGet<ArticleSummary[]>("/api/articles") })
  const [form, setForm] = useState<{ title?: string; category?: string; disposition?: string; target?: string }>({})
  const title = form.title ?? preview.data?.suggested_title ?? ""; const category = form.category ?? preview.data?.suggested_category ?? "concepts"; const disposition = form.disposition ?? preview.data?.suggested_disposition ?? "new"; const target = form.target ?? preview.data?.suggested_target ?? ""
  const commit = useMutation({
    mutationFn: () => apiPost<CommitResult>("/api/ingest/commit", { path, title, category, disposition, target_path: target }),
    onSuccess: async (result) => {
      await client.invalidateQueries()
      toast.success(
        disposition === "defer"
          ? "已暂缓"
          : disposition === "duplicate"
            ? "已记录为重复"
            : disposition === "seed"
              ? "原文已采用，可生成关键词已在正文中高亮"
              : result.task
              ? "Raw 已摄入，AI 编译已进入后台任务"
              : "Raw 已摄入",
      )
      const articlePath = result.article?.path ?? result.target_path
      navigate(articlePath ? `/${articlePath}` : "/inbox")
    },
    onError: (error) => toast.error(error.message),
  })
  if (preview.isLoading) return <PageFrame><Skeleton className="mx-auto h-96 max-w-5xl" /></PageFrame>
  if (preview.isError || !preview.data) return <PageFrame><div className="mx-auto max-w-3xl text-sm text-destructive">{preview.error?.message || "无法预览"}</div></PageFrame>
  const data = preview.data
  return <PageFrame><div className="mx-auto max-w-5xl"><PageTitle eyebrow={<StatusBadge value="Raw 摄入预览" kind="warn" />} title={data.suggested_title} description={<code className="break-all">{data.raw.path}</code>} />
    {data.raw.status === "integrity_changed" && <Alert variant="destructive" className="mb-6"><AlertTitle>Raw 完整性已变化</AlertTitle><AlertDescription>请将新版本另存为新文件，不能覆盖已摄入证据。</AlertDescription></Alert>}
    <div className="grid gap-8 lg:grid-cols-[22rem_minmax(0,1fr)]"><section><FieldGroup><Field><FieldLabel htmlFor="ingest-title">正本标题</FieldLabel><Input id="ingest-title" value={title} onChange={(event) => setForm((current) => ({ ...current, title: event.target.value }))} /></Field><Field><FieldLabel>分类</FieldLabel><Select value={category} onValueChange={(value) => value && setForm((current) => ({ ...current, category: value }))}><SelectTrigger className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{categories.data?.map((item) => <SelectItem key={item.id} value={item.id}>{item.label}</SelectItem>)}</SelectGroup></SelectContent></Select></Field><FieldSet><FieldLegend>处置</FieldLegend><RadioGroup value={disposition} onValueChange={(value) => value && setForm((current) => ({ ...current, disposition: value }))}>{[["seed","采用为 Wiki 种子"],["new","新建正本"],["supplement","补充正本"],["duplicate","记录重复"],["defer","暂缓"]].map(([value,label]) => <Field key={value} orientation="horizontal"><RadioGroupItem id={`disp-${value}`} value={value} /><FieldLabel htmlFor={`disp-${value}`}>{label}</FieldLabel></Field>)}</RadioGroup><FieldDescription>{disposition === "seed" ? "完整保留原文结构和内容，不经过 AI 摘要或改写。" : "重复与暂缓不会写 Wiki、index 或 log。"}</FieldDescription></FieldSet>{["supplement","duplicate"].includes(disposition) && <Field><FieldLabel>目标正本</FieldLabel><Select value={target} onValueChange={(value) => value && setForm((current) => ({ ...current, target: value }))}><SelectTrigger className="w-full"><SelectValue placeholder="选择正本" /></SelectTrigger><SelectContent><SelectGroup>{articles.data?.map((item) => <SelectItem key={item.path} value={item.path}>{item.title}</SelectItem>)}</SelectGroup></SelectContent></Select></Field>}</FieldGroup></section>
      <Tabs defaultValue="content"><TabsList><TabsTrigger value="content">正文预览</TabsTrigger><TabsTrigger value="changes">变更预览</TabsTrigger></TabsList><TabsContent value="content" className="mt-4 max-h-[60svh] overflow-y-auto border-l pl-6"><MarkdownContent markdown={data.markdown} fromPath={data.raw.path} /></TabsContent><TabsContent value="changes" className="mt-4"><ul className="flex flex-col gap-3 text-sm">{data.preview_changes.map((item) => <li key={item} className="border-b pb-3">更新 {item}</li>)}</ul></TabsContent></Tabs>
    </div><div className="sticky bottom-0 mt-8 flex justify-end gap-2 border-t bg-background/95 py-4 backdrop-blur"><Button variant="outline" onClick={() => navigate("/inbox")}>取消</Button><Button disabled={commit.isPending || data.raw.status === "integrity_changed"} onClick={() => commit.mutate()}>{commit.isPending ? <Spinner data-icon="inline-start" /> : <FileCheck2Icon data-icon="inline-start" />}确认处置</Button></div>
  </div></PageFrame>
}
