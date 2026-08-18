import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link, useNavigate, useSearchParams } from "react-router-dom"
import { EyeIcon, SendIcon, ShieldCheckIcon } from "lucide-react"
import { toast } from "sonner"

import { apiGet, apiPost, queryKeys, REUSE_POLICY_TEXT, REUSE_POLICY_VERSION, type Article, type PublicCategory, type PublicTag, type Submission } from "@/lib/api"
import { CategoryPicker, TagPicker, type TaxonomySelection } from "@/features/taxonomy-picker"
import { MarkdownContent } from "@/components/markdown-content"
import { PageFrame, PageTitle } from "@/components/page-frame"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { Checkbox } from "@/components/ui/checkbox"

type Preview = { preview_id: string; content_hash: string; source_revision: string; snapshot: Submission["snapshot"]; taxonomy: Submission["taxonomy"] }

export function SharePage() {
  const [params] = useSearchParams()
  const path = params.get("article") ?? ""
  const [attribution, setAttribution] = useState("nickname")
  const [publicCategory, setPublicCategory] = useState<TaxonomySelection | null>(null)
  const [selectedTags, setSelectedTags] = useState<TaxonomySelection[]>([])
  const [reusePermission, setReusePermission] = useState("view_only")
  const [reuseAcknowledged, setReuseAcknowledged] = useState(false)
  const [linkProfile, setLinkProfile] = useState(false)
  const [sourceUrls, setSourceUrls] = useState<string[]>([])
  const [preview, setPreview] = useState<Preview | null>(null)
  const article = useQuery({ queryKey: queryKeys.article(path), queryFn: () => apiGet<Article>(`/api/article?path=${encodeURIComponent(path)}`), enabled: Boolean(path) })
  const categories = useQuery({ queryKey: queryKeys.publicCategories, queryFn: () => apiGet<PublicCategory[]>("/api/public/categories") })
  const tags = useQuery({ queryKey: queryKeys.publicTags, queryFn: () => apiGet<PublicTag[]>("/api/public/tags") })
  const navigate = useNavigate(); const client = useQueryClient()
  const createPreview = useMutation({ mutationFn: () => apiPost<Preview>("/api/share-previews", { article_path: path, source_revision: article.data!.revision, attribution, category_selection: publicCategory?.kind === "existing" ? { kind: "existing", id: publicCategory.id } : { kind: "proposal", name: publicCategory?.name }, tag_selections: selectedTags.map((tag) => tag.kind === "existing" ? { kind: "existing", id: tag.id } : { kind: "proposal", name: tag.name }), reuse_permission: reusePermission, reuse_policy_version: REUSE_POLICY_VERSION, reuse_policy_acknowledged: reusePermission === "allow_private_copy" && reuseAcknowledged, link_public_profile: linkProfile, source_urls: sourceUrls }), onSuccess: setPreview, onError: (error) => toast.error(error.message) })
  const submit = useMutation({ mutationFn: () => apiPost<Submission>("/api/submissions", { preview_id: preview!.preview_id }), onSuccess: async (result) => { await Promise.all([client.invalidateQueries({ queryKey: queryKeys.submissions }), client.invalidateQueries({ queryKey: queryKeys.article(path) })]); toast.success(article.data?.publication.state === "relist_available" ? "重新上架申请已进入 AI 预审" : article.data?.publication.state === "update_available" ? "更新已进入 AI 预审" : "投稿已进入 AI 预审"); navigate(`/submissions/${result.id}`) }, onError: (error) => toast.error(error.message) })
  if (article.isLoading) return <PageFrame><Skeleton className="mx-auto h-96 max-w-3xl" /></PageFrame>
  if (!article.data) return <PageFrame><Alert variant="destructive"><AlertTitle>无法创建投稿</AlertTitle><AlertDescription>词条不存在或不属于当前空间。</AlertDescription></Alert></PageFrame>
  const isUpdate = article.data.publication.state === "update_available"
  const isRelist = article.data.publication.state === "relist_available"
  return <PageFrame><div className="mx-auto max-w-4xl"><PageTitle eyebrow="Wiki 广场" title={isRelist ? "申请重新上架" : isUpdate ? "提交广场更新" : "分享确认"} description={isRelist ? "当前修改会固化为新的投稿快照，并重新经过 AI 预审和 Admin 审核；被下架版本不会被改写。" : isUpdate ? `广场版本 ${article.data.publication.public_version} 会继续展示，直到本次更新重新通过 AI 预审和 Admin 审核。` : "确认页展示的内容将按字节固化；后续私有编辑不会改变这次投稿。"} />
    {!preview ? <div className="space-y-8"><Alert><ShieldCheckIcon /><AlertTitle>最小披露</AlertTitle><AlertDescription>只提交标题、分类、内容状态、正文、公开署名和来源摘要；Raw 全文、私有路径、反链、任务与模型配置不会提交。</AlertDescription></Alert>
      <div><h2 className="text-sm font-medium">公开署名</h2><RadioGroup className="mt-3" value={attribution} onValueChange={(value) => value && setAttribution(value)}><div className="flex items-center gap-2"><RadioGroupItem value="nickname" id="nickname" /><Label htmlFor="nickname">使用账号昵称</Label></div><div className="flex items-center gap-2"><RadioGroupItem value="anonymous" id="anonymous" /><Label htmlFor="anonymous">匿名发布</Label></div></RadioGroup>{attribution === "nickname" && <label className="mt-3 flex items-center gap-2 text-sm"><Checkbox checked={linkProfile} onCheckedChange={(value) => setLinkProfile(Boolean(value))} />关联已启用的公开作者主页</label>}</div>
      <div className="grid gap-6 sm:grid-cols-2"><div><h2 className="mb-3 text-sm font-medium">公共主分类</h2><CategoryPicker options={(categories.data ?? []).flatMap((item) => item.id ? [{ id: item.id, name: item.name }] : [])} value={publicCategory} onChange={setPublicCategory} createKind="proposal" /><p className="mt-2 text-xs text-muted-foreground">新名称仅作为本次投稿提案，批准前不会进入公共导航。</p></div><div><h2 className="mb-3 text-sm font-medium">公开标签</h2><TagPicker options={(tags.data ?? []).map((tag) => ({ id: tag.id, name: tag.name }))} value={selectedTags} onChange={setSelectedTags} createKind="proposal" maximum={3} /></div></div>
      <div><h2 className="text-sm font-medium">私人复用许可</h2><RadioGroup className="mt-3" value={reusePermission} onValueChange={(value) => { if (!value) return; setReusePermission(value); setReuseAcknowledged(false) }}><div className="flex items-center gap-2"><RadioGroupItem value="view_only" id="view-only" /><Label htmlFor="view-only">仅允许公开阅读</Label></div><div className="flex items-center gap-2"><RadioGroupItem value="allow_private_copy" id="allow-copy" /><Label htmlFor="allow-copy">允许登录用户复制到私人 Wiki</Label></div></RadioGroup>{reusePermission === "allow_private_copy" && <Alert className="mt-3"><AlertTitle>允许复制到私人 Wiki</AlertTitle><AlertDescription><p>{REUSE_POLICY_TEXT}</p><label className="mt-3 flex items-start gap-2 text-foreground"><Checkbox checked={reuseAcknowledged} onCheckedChange={(value) => setReuseAcknowledged(Boolean(value))} /><span>我已阅读并确认上述许可（{REUSE_POLICY_VERSION}）</span></label></AlertDescription></Alert>}</div>
      {!!article.data.sources?.length && <div><h2 className="text-sm font-medium">公开来源</h2><p className="mt-1 text-sm text-muted-foreground">只可选择正文已有的 HTTP(S) 公共链接；私人 Wiki、Raw 和相对路径不会公开。平台不会抓取或核验第三方页面。</p><div className="mt-3 space-y-2">{article.data.sources.map((source) => { const url = source.match(/https?:\/\/[^\s<>]+/)?.[0]; return url ? <label key={url} className="flex items-start gap-2 text-sm"><Checkbox checked={sourceUrls.includes(url)} onCheckedChange={(value) => setSourceUrls((current) => value ? [...current, url] : current.filter((item) => item !== url))} /><span className="break-all">{source}</span></label> : null })}</div></div>}
      <div className="flex justify-end"><Button disabled={!publicCategory || createPreview.isPending || (reusePermission === "allow_private_copy" && !reuseAcknowledged)} onClick={() => createPreview.mutate()}>{createPreview.isPending ? <Spinner data-icon="inline-start" /> : <EyeIcon data-icon="inline-start" />}生成精确预览</Button></div>
    </div> : <div className="space-y-8"><div className="rounded-lg border p-5"><div className="text-xs text-muted-foreground">快照哈希 {preview.content_hash.slice(0, 16)}</div><h2 className="mt-3 text-2xl font-semibold">{preview.snapshot.title}</h2>{preview.taxonomy && <div className="mt-4 flex flex-wrap gap-2 text-xs text-muted-foreground"><span>主分类：{preview.taxonomy.category.name}{preview.taxonomy.category.kind === "proposal" ? "（提案）" : ""}</span>{preview.taxonomy.tags.map((tag) => <span key={tag.kind === "proposal" ? tag.key : tag.id}>#{tag.name}{tag.kind === "proposal" ? "（提案）" : ""}</span>)}</div>}<MarkdownContent className="mt-8" markdown={preview.snapshot.markdown.replace(/^#\s+.*\n/, "")} fromPath="" publicMode /></div>
      <div className="flex flex-wrap justify-between gap-3"><Button variant="outline" onClick={() => setPreview(null)}>返回修改选项</Button><Button disabled={submit.isPending} onClick={() => submit.mutate()}>{submit.isPending ? <Spinner data-icon="inline-start" /> : <SendIcon data-icon="inline-start" />}提交 AI 预审</Button></div></div>}
    <div className="mt-8 text-sm"><Link className="text-link" to={`/${path}`}>返回私有词条</Link></div>
  </div></PageFrame>
}
