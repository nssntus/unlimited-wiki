import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link, useNavigate, useSearchParams } from "react-router-dom"
import { EyeIcon, SendIcon, ShieldCheckIcon } from "lucide-react"
import { toast } from "sonner"

import { apiGet, apiPost, queryKeys, type Article, type Submission } from "@/lib/api"
import { MarkdownContent } from "@/components/markdown-content"
import { PageFrame, PageTitle } from "@/components/page-frame"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"

type Preview = { preview_id: string; content_hash: string; source_revision: string; snapshot: Submission["snapshot"] }

export function SharePage() {
  const [params] = useSearchParams()
  const path = params.get("article") ?? ""
  const [attribution, setAttribution] = useState("nickname")
  const [preview, setPreview] = useState<Preview | null>(null)
  const article = useQuery({ queryKey: queryKeys.article(path), queryFn: () => apiGet<Article>(`/api/article?path=${encodeURIComponent(path)}`), enabled: Boolean(path) })
  const navigate = useNavigate(); const client = useQueryClient()
  const createPreview = useMutation({ mutationFn: () => apiPost<Preview>("/api/share-previews", { article_path: path, source_revision: article.data!.revision, attribution }), onSuccess: setPreview, onError: (error) => toast.error(error.message) })
  const submit = useMutation({ mutationFn: () => apiPost<Submission>("/api/submissions", { preview_id: preview!.preview_id }), onSuccess: async (result) => { await Promise.all([client.invalidateQueries({ queryKey: queryKeys.submissions }), client.invalidateQueries({ queryKey: queryKeys.article(path) })]); toast.success(article.data?.publication.state === "relist_available" ? "重新上架申请已进入 AI 预审" : article.data?.publication.state === "update_available" ? "更新已进入 AI 预审" : "投稿已进入 AI 预审"); navigate(`/submissions/${result.id}`) }, onError: (error) => toast.error(error.message) })
  if (article.isLoading) return <PageFrame><Skeleton className="mx-auto h-96 max-w-3xl" /></PageFrame>
  if (!article.data) return <PageFrame><Alert variant="destructive"><AlertTitle>无法创建投稿</AlertTitle><AlertDescription>词条不存在或不属于当前空间。</AlertDescription></Alert></PageFrame>
  const isUpdate = article.data.publication.state === "update_available"
  const isRelist = article.data.publication.state === "relist_available"
  return <PageFrame><div className="mx-auto max-w-4xl"><PageTitle eyebrow="Wiki 广场" title={isRelist ? "申请重新上架" : isUpdate ? "提交广场更新" : "分享确认"} description={isRelist ? "当前修改会固化为新的投稿快照，并重新经过 AI 预审和 Admin 审核；被下架版本不会被改写。" : isUpdate ? `广场版本 ${article.data.publication.public_version} 会继续展示，直到本次更新重新通过 AI 预审和 Admin 审核。` : "确认页展示的内容将按字节固化；后续私有编辑不会改变这次投稿。"} />
    {!preview ? <div className="space-y-8"><Alert><ShieldCheckIcon /><AlertTitle>最小披露</AlertTitle><AlertDescription>只提交标题、分类、内容状态、正文、公开署名和来源摘要；Raw 全文、私有路径、反链、任务与模型配置不会提交。</AlertDescription></Alert>
      <div><h2 className="text-sm font-medium">公开署名</h2><RadioGroup className="mt-3" value={attribution} onValueChange={(value) => value && setAttribution(value)}><div className="flex items-center gap-2"><RadioGroupItem value="nickname" id="nickname" /><Label htmlFor="nickname">使用账号昵称</Label></div><div className="flex items-center gap-2"><RadioGroupItem value="anonymous" id="anonymous" /><Label htmlFor="anonymous">匿名发布</Label></div></RadioGroup></div>
      <div className="flex justify-end"><Button disabled={createPreview.isPending} onClick={() => createPreview.mutate()}>{createPreview.isPending ? <Spinner data-icon="inline-start" /> : <EyeIcon data-icon="inline-start" />}生成精确预览</Button></div>
    </div> : <div className="space-y-8"><div className="rounded-lg border p-5"><div className="text-xs text-muted-foreground">快照哈希 {preview.content_hash.slice(0, 16)}</div><h2 className="mt-3 text-2xl font-semibold">{preview.snapshot.title}</h2><MarkdownContent className="mt-8" markdown={preview.snapshot.markdown.replace(/^#\s+.*\n/, "")} fromPath="" publicMode /></div>
      <div className="flex flex-wrap justify-between gap-3"><Button variant="outline" onClick={() => setPreview(null)}>返回修改选项</Button><Button disabled={submit.isPending} onClick={() => submit.mutate()}>{submit.isPending ? <Spinner data-icon="inline-start" /> : <SendIcon data-icon="inline-start" />}提交 AI 预审</Button></div></div>}
    <div className="mt-8 text-sm"><Link className="text-link" to={`/${path}`}>返回私有词条</Link></div>
  </div></PageFrame>
}
