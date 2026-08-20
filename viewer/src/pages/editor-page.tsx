import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useLocation, useNavigate } from "react-router-dom"
import { SaveIcon } from "lucide-react"
import { toast } from "sonner"

import { ApiError, apiGet, apiPost, type Article, type PrivateTaxonomy, queryKeys } from "@/lib/api"
import { CategoryPicker, TagPicker, type TaxonomySelection } from "@/features/taxonomy-picker"
import { MarkdownEditor } from "@/features/markdown-editor"
import { PageFrame, PageTitle } from "@/components/page-frame"
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"

type SaveResponse = { conflict: boolean; article?: Article; disk?: Article; operation_id?: string }

export function EditorPage() {
  const path = decodeURIComponent(useLocation().pathname.replace(/^\/edit\//, "")); const navigate = useNavigate(); const client = useQueryClient()
  const article = useQuery({ queryKey: queryKeys.article(path), queryFn: () => apiGet<Article>(`/api/article?path=${encodeURIComponent(path)}`) })
  const taxonomy = useQuery({ queryKey: queryKeys.taxonomy, queryFn: () => apiGet<PrivateTaxonomy>("/api/taxonomy") })
  const [draft, setDraft] = useState<string | null>(null); const [conflict, setConflict] = useState<Article | null>(null)
  const [categoryChoice, setCategoryChoice] = useState<TaxonomySelection | null>(null)
  const [tagChoices, setTagChoices] = useState<TaxonomySelection[] | null>(null)
  const markdown = draft ?? article.data?.markdown ?? ""
  const effectiveCategory = categoryChoice ?? (article.data?.primary_category_id ? { kind: "existing" as const, id: article.data.primary_category_id, name: article.data.category_label } : { kind: "inbox" as const, name: "暂不分类" })
  const effectiveTags = tagChoices ?? (article.data?.tags ?? []).map((name) => ({ kind: "existing" as const, id: name.normalize("NFKC").toLocaleLowerCase(), name }))
  const save = useMutation<SaveResponse, Error, boolean>({ mutationFn: (force) => apiPost<SaveResponse>("/api/article/save", { path, markdown, revision: article.data?.revision || "", force, category: effectiveCategory.kind === "existing" ? { kind: "existing", id: effectiveCategory.id } : effectiveCategory.kind === "inbox" ? { kind: "inbox" } : { kind: "create", name: effectiveCategory.name }, tags: effectiveTags.map((tag) => tag.name) }), onSuccess: (result) => { if (result.conflict && result.disk) { setConflict(result.disk); return } void client.invalidateQueries(); toast.success("正文与分类已保存"); navigate(`/${result.article?.path || path}`) }, onError: (error) => { if (error instanceof ApiError && error.status === 409) toast.error("磁盘版本已变化"); else toast.error(error.message) } })
  if (article.isLoading) return <PageFrame><Skeleton className="mx-auto h-[70svh] max-w-6xl" /></PageFrame>
  if (!article.data) return <PageFrame><p className="text-sm text-destructive">{article.error?.message}</p></PageFrame>
  const dirty = markdown !== article.data.markdown || categoryChoice !== null || tagChoices !== null
  return <PageFrame><div className="mx-auto max-w-6xl"><PageTitle eyebrow="正文治理" title={`编辑 ${article.data.title}`} description="Markdown 是事实源；预览使用与阅读页相同的安全渲染边界。" actions={<><Button variant="outline" onClick={() => navigate(`/${path}`)}>取消</Button><Button disabled={!dirty || save.isPending} onClick={() => save.mutate(false)}>{save.isPending ? <Spinner data-icon="inline-start" /> : <SaveIcon data-icon="inline-start" />}保存</Button></>} />
    <section className="mb-5 grid gap-4 border-y py-4 sm:grid-cols-2"><div><h2 className="mb-2 text-sm font-medium">主分类</h2><CategoryPicker options={(taxonomy.data?.categories ?? []).map((item) => ({ id: item.id, name: item.name }))} value={effectiveCategory} onChange={setCategoryChoice} allowInbox /></div><div><h2 className="mb-2 text-sm font-medium">标签</h2><TagPicker options={(taxonomy.data?.tags ?? []).map((name) => ({ id: name.normalize("NFKC").toLocaleLowerCase(), name }))} value={effectiveTags} onChange={setTagChoices} /></div></section>
    <MarkdownEditor value={markdown} onChange={setDraft} fromPath={path} />
    <AlertDialog open={Boolean(conflict)} onOpenChange={(open) => !open && setConflict(null)}><AlertDialogContent className="max-w-3xl"><AlertDialogHeader><AlertDialogTitle>正文已在磁盘上变化</AlertDialogTitle><AlertDialogDescription>你的版本不会自动覆盖磁盘版本。可重载磁盘内容，或明确强制保存。</AlertDialogDescription></AlertDialogHeader><div className="grid max-h-[50dvh] gap-4 overflow-y-auto sm:grid-cols-2"><div><h3 className="mb-2 text-sm font-medium">你的版本</h3><pre className="whitespace-pre-wrap rounded-md bg-muted p-3 text-xs">{markdown.slice(0, 5000)}</pre></div><div><h3 className="mb-2 text-sm font-medium">磁盘版本</h3><pre className="whitespace-pre-wrap rounded-md bg-muted p-3 text-xs">{conflict?.markdown.slice(0, 5000)}</pre></div></div><AlertDialogFooter><AlertDialogCancel onClick={() => { if (conflict) setDraft(conflict.markdown); setConflict(null) }}>重载磁盘</AlertDialogCancel><AlertDialogAction onClick={() => { setConflict(null); save.mutate(true) }}>强制保存</AlertDialogAction></AlertDialogFooter></AlertDialogContent></AlertDialog>
  </div></PageFrame>
}
