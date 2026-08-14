import { useEffect, useMemo, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  CheckCheckIcon,
  EyeIcon,
  FolderPlusIcon,
  RefreshCwIcon,
  TagsIcon,
  Undo2Icon,
  XIcon,
} from "lucide-react"
import { toast } from "sonner"

import { StatusBadge } from "@/components/markdown-content"
import { PageFrame, PageTitle } from "@/components/page-frame"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { OperationRollbackAlert } from "@/features/operation-rollback-alert"
import {
  ApiError,
  apiGet,
  apiPost,
  type Article,
  type Category,
  queryKeys,
} from "@/lib/api"

type Candidate = { category_id: string; confidence: number; reason: string }
type WorkItem = {
  article: Article & { summary: string }
  group: string
  suggestion: {
    status: string
    suggestion: {
      candidates: Candidate[]
      tags: string[]
      new_category: { name: string; description: string } | null
    } | null
    error_type?: string
    error_message?: string
  } | null
}
type Selection = {
  article_id: string
  article_revision: string
  decision: "existing" | "new" | "defer"
  category_id?: string
  new_category?: InlineCategory
  tags: string[]
}
type Workbench = {
  counts: Record<string, number>
  high_confidence_threshold: number
  draft: { revision: number; selections: Selection[] }
  items: WorkItem[]
}
type InlineCategory = {
  client_ref: string
  name: string
  description: string
}

function isActionable(selection: Selection) {
  return selection.decision === "existing" || selection.decision === "new"
}

function recommendedSelection(item: WorkItem): Selection | null {
  const candidate = item.suggestion?.suggestion?.candidates?.[0]
  if (!candidate) return null
  return {
    article_id: item.article.article_id,
    article_revision: item.article.revision,
    decision: "existing",
    category_id: candidate.category_id,
    tags: item.suggestion?.suggestion?.tags ?? item.article.tags,
  }
}

export function ClassificationPage() {
  const client = useQueryClient()
  const workbench = useQuery({
    queryKey: queryKeys.classifications,
    queryFn: () => apiGet<Workbench>("/api/classifications/workbench"),
    refetchInterval: 5000,
  })
  const categories = useQuery({
    queryKey: queryKeys.categories,
    queryFn: () => apiGet<Category[]>("/api/categories"),
  })
  const [selected, setSelected] = useState<Record<string, Selection>>({})
  const [draftRevision, setDraftRevision] = useState(0)
  const [draftConflict, setDraftConflict] = useState(false)
  const [operationId, setOperationId] = useState<string | null>(null)
  const [inlineCategories, setInlineCategories] = useState<InlineCategory[]>([])
  const [newName, setNewName] = useState("")
  const [newDescription, setNewDescription] = useState("")
  const [bulkTags, setBulkTags] = useState("")
  const draftHydrated = useRef(false)
  const savedDraftPayload = useRef("")
  const [preview, setPreview] = useState<{
    preview_id: string
    moves: { source_path: string; target_path: string }[]
    creates: Category[]
    conflicts: { kind: string }[]
    can_commit: boolean
  } | null>(null)
  const actionable = useMemo(
    () => Object.values(selected).filter(isActionable),
    [selected]
  )
  const selectedCount = actionable.length
  const categoryNames = useMemo(
    () => new Map(categories.data?.map((item) => [item.category_id, item.name])),
    [categories.data]
  )

  function loadDraft(data: Workbench) {
    let selections = data.draft.selections
    if (data.draft.revision === 0 && selections.length === 0) {
      selections = data.items
        .filter((item) => item.group === "high_confidence")
        .map(recommendedSelection)
        .filter((item): item is Selection => Boolean(item))
    }
    setSelected(Object.fromEntries(selections.map((item) => [item.article_id, item])))
    setDraftRevision(data.draft.revision)
    savedDraftPayload.current = JSON.stringify(data.draft.selections)
    setDraftConflict(false)
  }

  useEffect(() => {
    if (!workbench.data || draftHydrated.current) return
    loadDraft(workbench.data)
    draftHydrated.current = true
  }, [workbench.data])

  useEffect(() => {
    if (!draftHydrated.current || draftConflict) return
    const selections = Object.values(selected)
    const serialized = JSON.stringify(selections)
    if (serialized === savedDraftPayload.current) return
    const timer = window.setTimeout(() => {
      void apiPost<{ revision: number }>(
        "/api/classifications/draft",
        { selections, expected_revision: draftRevision },
        false
      )
        .then((result) => {
          savedDraftPayload.current = serialized
          setDraftRevision(result.revision)
        })
        .catch((error: Error) => {
          if (error instanceof ApiError && error.status === 409) setDraftConflict(true)
          toast.error(error.message)
        })
    }, 400)
    return () => window.clearTimeout(timer)
  }, [draftConflict, draftRevision, selected])

  const previewMutation = useMutation({
    mutationFn: () =>
      apiPost<typeof preview>(
        "/api/classifications/preview",
        { selections: actionable },
        false
      ),
    onSuccess: setPreview,
    onError: (error) => toast.error(error.message),
  })
  const commit = useMutation({
    mutationFn: () =>
      apiPost<{ operation_id: string }>("/api/classifications/commit", {
        preview_id: preview?.preview_id,
      }),
    onSuccess: async (result) => {
      setOperationId(result.operation_id)
      toast.success(`归类完成 · ${result.operation_id}`)
      setPreview(null)
      savedDraftPayload.current = "[]"
      setDraftRevision(0)
      setSelected({})
      await client.invalidateQueries()
    },
    onError: async (error) => {
      if (error instanceof ApiError && error.status === 409) {
        setPreview(null)
        await workbench.refetch()
      }
      toast.error(error.message)
    },
  })
  const retry = useMutation({
    mutationFn: (articleId: string) =>
      apiPost("/api/classifications/retry", { article_id: articleId }),
    onSuccess: async () => {
      toast.success("归类建议已重新入队")
      await client.invalidateQueries({ queryKey: queryKeys.classifications })
    },
    onError: (error) => toast.error(error.message),
  })

  function choose(item: WorkItem, categoryId: string) {
    const tags = selected[item.article.article_id]?.tags ?? item.suggestion?.suggestion?.tags ?? item.article.tags
    setSelected((current) => ({
      ...current,
      [item.article.article_id]: {
        article_id: item.article.article_id,
        article_revision: item.article.revision,
        decision: "existing",
        category_id: categoryId,
        tags,
      },
    }))
  }

  function selectHighConfidence() {
    setSelected((current) => {
      const next = { ...current }
      for (const item of workbench.data?.items ?? []) {
        const selection = recommendedSelection(item)
        const confidence = item.suggestion?.suggestion?.candidates?.[0]?.confidence ?? 0
        if (selection && confidence >= (workbench.data?.high_confidence_threshold ?? 0.85)) {
          next[item.article.article_id] = selection
        }
      }
      return next
    })
  }

  function setDeferred(item: WorkItem) {
    setSelected((current) => ({
      ...current,
      [item.article.article_id]: {
        article_id: item.article.article_id,
        article_revision: item.article.revision,
        decision: "defer",
        tags: current[item.article.article_id]?.tags ?? item.article.tags,
      },
    }))
  }

  function removeSelection(articleId: string) {
    setSelected((current) => {
      const next = { ...current }
      delete next[articleId]
      return next
    })
  }

  function applyInlineCategory(item: WorkItem, spec: InlineCategory) {
    setSelected((current) => ({
      ...current,
      [item.article.article_id]: {
        article_id: item.article.article_id,
        article_revision: item.article.revision,
        decision: "new",
        new_category: spec,
        tags: current[item.article.article_id]?.tags ?? item.suggestion?.suggestion?.tags ?? item.article.tags,
      },
    }))
  }

  function updateInlineCategory(clientRef: string, change: Partial<InlineCategory>) {
    setInlineCategories((current) =>
      current.map((item) => item.client_ref === clientRef ? { ...item, ...change } : item)
    )
    setSelected((current) => Object.fromEntries(Object.entries(current).map(([id, item]) => [
      id,
      item.new_category?.client_ref === clientRef
        ? { ...item, new_category: { ...item.new_category, ...change } }
        : item,
    ])))
  }

  function applyBulkCategory(categoryId: string) {
    const items = new Map(workbench.data?.items.map((item) => [item.article.article_id, item]))
    setSelected((current) => Object.fromEntries(Object.entries(current).map(([id, selection]) => {
      const item = items.get(id)
      if (!item || !isActionable(selection)) return [id, selection]
      return [id, { ...selection, decision: "existing", category_id: categoryId, new_category: undefined }]
    })))
  }

  function applyBulkTags() {
    const tags = bulkTags.split(";").map((item) => item.trim()).filter(Boolean)
    if (!tags.length) return
    setSelected((current) => Object.fromEntries(Object.entries(current).map(([id, selection]) => [
      id,
      isActionable(selection) ? { ...selection, tags: [...new Set([...selection.tags, ...tags])] } : selection,
    ])))
  }

  if (workbench.isLoading || categories.isLoading) {
    return <PageFrame><Skeleton className="h-96" /></PageFrame>
  }
  if (workbench.isError || categories.isError) {
    const error = workbench.error ?? categories.error
    return (
      <PageFrame>
        <Alert variant="destructive">
          <AlertTitle>待归类工作台加载失败</AlertTitle>
          <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
            <span>{error?.message}</span>
            <Button variant="outline" onClick={() => { void workbench.refetch(); void categories.refetch() }}>
              <RefreshCwIcon data-icon="inline-start" />重试
            </Button>
          </AlertDescription>
        </Alert>
      </PageFrame>
    )
  }

  return (
    <PageFrame>
      <div className="mx-auto max-w-6xl">
        <PageTitle
          title="待归类"
          description="AI 基于完整正文给出建议；选择只保存在工作台，预览并确认后才会创建目录和移动文件。"
          actions={<Button variant="outline" onClick={selectHighConfidence}><CheckCheckIcon data-icon="inline-start" />选择 ≥85% 推荐</Button>}
        />
        <OperationRollbackAlert operationId={operationId} label="批量归类" onRolledBack={() => setOperationId(null)} />
        {draftConflict && (
          <Alert variant="destructive" className="mb-6">
            <AlertTitle>工作台草稿已在其他页面更新</AlertTitle>
            <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
              <span>自动保存已暂停，加载服务器草稿后再继续。</span>
              <Button variant="outline" onClick={() => void workbench.refetch().then((result) => result.data && loadDraft(result.data))}>加载服务器草稿</Button>
            </AlertDescription>
          </Alert>
        )}
        <section className="mb-6 border-y py-4">
          <h2 className="text-sm font-medium">批量处理已选择词条</h2>
          <div className="mt-3 grid gap-3 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto]">
            <Select onValueChange={(value) => typeof value === "string" && value && applyBulkCategory(value)}>
              <SelectTrigger className="w-full"><SelectValue placeholder="批量指定主分类" /></SelectTrigger>
              <SelectContent><SelectGroup>{categories.data?.map((category) => <SelectItem key={category.category_id} value={category.category_id}>{category.name}</SelectItem>)}</SelectGroup></SelectContent>
            </Select>
            <Input value={bulkTags} onChange={(event) => setBulkTags(event.target.value)} placeholder="批量追加标签，以分号分隔" />
            <Button variant="outline" disabled={!selectedCount || !bulkTags.trim()} onClick={applyBulkTags}><TagsIcon data-icon="inline-start" />追加标签</Button>
          </div>
        </section>
        <section className="mb-6 border-y py-4">
          <h2 className="text-sm font-medium">内联新建分类草稿</h2>
          <p className="mt-1 text-sm text-muted-foreground">这里只保存草稿；被词条采用并提交后才创建真实目录。</p>
          <div className="mt-3 grid gap-3 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto]">
            <Input value={newName} onChange={(event) => setNewName(event.target.value)} placeholder="分类名称" />
            <Input value={newDescription} onChange={(event) => setNewDescription(event.target.value)} placeholder="分类说明" />
            <Button variant="outline" disabled={!newName.trim()} onClick={() => {
              setInlineCategories((current) => [...current, { client_ref: crypto.randomUUID(), name: newName.trim(), description: newDescription.trim() }])
              setNewName(""); setNewDescription("")
            }}><FolderPlusIcon data-icon="inline-start" />加入草稿</Button>
          </div>
          {inlineCategories.map((spec) => (
            <div key={spec.client_ref} className="mt-3 grid gap-3 border-t pt-3 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto]">
              <Input aria-label="新分类名称" value={spec.name} onChange={(event) => updateInlineCategory(spec.client_ref, { name: event.target.value })} />
              <Input aria-label="新分类说明" value={spec.description} onChange={(event) => updateInlineCategory(spec.client_ref, { description: event.target.value })} />
              <Button variant="outline" disabled={!selectedCount || !spec.name.trim()} onClick={() => {
                const items = new Map(workbench.data?.items.map((item) => [item.article.article_id, item]))
                for (const selection of actionable) { const item = items.get(selection.article_id); if (item) applyInlineCategory(item, spec) }
              }}>应用到 {selectedCount} 项</Button>
            </div>
          ))}
        </section>
        {!workbench.data?.items.length ? (
          <Alert><AlertTitle>没有待归类词条</AlertTitle><AlertDescription>新生成的正本会先进入这里。</AlertDescription></Alert>
        ) : (
          <div className="divide-y border-y">
            {workbench.data.items.map((item) => {
              const suggestion = item.suggestion?.suggestion
              const current = selected[item.article.article_id]
              const checked = Boolean(current && isActionable(current))
              return (
                <section key={item.article.article_id} className="grid gap-4 py-5 lg:grid-cols-[minmax(0,1fr)_20rem]">
                  <div>
                    <div className="flex items-center gap-3">
                      <Checkbox aria-label={`选择 ${item.article.title}`} checked={checked} onCheckedChange={(value) => {
                        if (!value) return setDeferred(item)
                        const recommended = recommendedSelection(item)
                        if (recommended) setSelected((state) => ({ ...state, [item.article.article_id]: recommended }))
                        else if (categories.data?.[0]) choose(item, categories.data[0].category_id)
                      }} />
                      <h2 className="font-medium">{item.article.title}</h2>
                      <StatusBadge value={item.group === "high_confidence" ? "高置信" : item.group === "failed" ? "建议失败" : "需确认"} kind={item.group === "high_confidence" ? "good" : "warn"} />
                    </div>
                    <p className="mt-2 line-clamp-3 text-sm text-muted-foreground">{item.article.summary || "暂无摘要"}</p>
                    <code className="mt-2 block text-xs break-all text-muted-foreground">{item.article.path}</code>
                    {suggestion?.candidates?.map((candidate) => (
                      <div key={candidate.category_id} className="mt-3 text-sm">
                        <span className="font-medium">{categoryNames.get(candidate.category_id) ?? candidate.category_id} · {Math.round(candidate.confidence * 100)}%</span>
                        <p className="text-muted-foreground">{candidate.reason}</p>
                      </div>
                    ))}
                    {item.suggestion?.status === "failed" && <div className="mt-3"><p className="text-sm text-destructive">{item.suggestion.error_type} · {item.suggestion.error_message}</p><Button className="mt-2" size="sm" variant="outline" onClick={() => retry.mutate(item.article.article_id)}>重试 AI 建议</Button></div>}
                  </div>
                  <div className="flex flex-col gap-3">
                    <Select value={current?.category_id ?? ""} onValueChange={(value) => value && choose(item, value)}>
                      <SelectTrigger className="w-full"><SelectValue placeholder="选择现有分类" /></SelectTrigger>
                      <SelectContent><SelectGroup>{categories.data?.map((category) => <SelectItem key={category.category_id} value={category.category_id}>{category.name}</SelectItem>)}</SelectGroup></SelectContent>
                    </Select>
                    {suggestion?.new_category && <Button variant="outline" onClick={() => {
                      const spec = { client_ref: `ai-${item.article.article_id}`, name: suggestion.new_category!.name, description: suggestion.new_category!.description }
                      setInlineCategories((currentSpecs) => currentSpecs.some((entry) => entry.client_ref === spec.client_ref) ? currentSpecs : [...currentSpecs, spec])
                      applyInlineCategory(item, spec)
                    }}><FolderPlusIcon data-icon="inline-start" />采用并编辑建议新分类</Button>}
                    {inlineCategories.map((spec) => <Button key={spec.client_ref} size="sm" variant="ghost" disabled={!spec.name.trim()} onClick={() => applyInlineCategory(item, spec)}>使用新类：{spec.name || "未命名"}</Button>)}
                    <Input aria-label={`${item.article.title} 标签`} placeholder="标签以分号分隔" value={(current?.tags ?? suggestion?.tags ?? []).join("; ")} onChange={(event) => {
                      const tags = event.target.value.split(";").map((value) => value.trim()).filter(Boolean)
                      const base = current ?? { article_id: item.article.article_id, article_revision: item.article.revision, decision: "defer" as const, tags: [] }
                      setSelected((state) => ({ ...state, [item.article.article_id]: { ...base, tags } }))
                    }} />
                    <div className="flex flex-wrap gap-2">
                      <Button size="sm" variant="ghost" onClick={() => setDeferred(item)}>暂不处理</Button>
                      {current && <Button size="sm" variant="ghost" onClick={() => removeSelection(item.article.article_id)}><XIcon data-icon="inline-start" />移除选择</Button>}
                    </div>
                  </div>
                </section>
              )
            })}
          </div>
        )}
        <div className="sticky bottom-0 mt-6 flex items-center justify-between border-t bg-background/95 py-4 backdrop-blur">
          <span className="text-sm text-muted-foreground">可提交 {selectedCount} 项</span>
          <Button disabled={!selectedCount || previewMutation.isPending || draftConflict} onClick={() => previewMutation.mutate()}><EyeIcon data-icon="inline-start" />预览变更</Button>
        </div>
        <Dialog open={Boolean(preview)} onOpenChange={(open) => !open && setPreview(null)}>
          <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-2xl">
            <DialogHeader><DialogTitle>确认批量归类</DialogTitle><DialogDescription>提交后将原子移动文件、更新元数据、链接、索引和操作日志。</DialogDescription></DialogHeader>
            {preview?.creates.map((item) => <Alert key={item.category_id}><FolderPlusIcon /><AlertTitle>创建分类 {item.name}</AlertTitle><AlertDescription>{item.directory_name}</AlertDescription></Alert>)}
            <div className="divide-y">{preview?.moves.map((move) => <div key={move.source_path} className="py-3 text-sm"><code className="break-all">{move.source_path}</code><span className="mx-2">→</span><code className="break-all">{move.target_path}</code></div>)}</div>
            {preview?.conflicts.length ? <Alert variant="destructive"><AlertTitle>存在阻断冲突</AlertTitle><AlertDescription>{preview.conflicts.map((item) => item.kind).join("、")}</AlertDescription></Alert> : null}
            <DialogFooter><Button variant="outline" onClick={() => setPreview(null)}><Undo2Icon data-icon="inline-start" />返回修改</Button><Button disabled={!preview?.can_commit || commit.isPending} onClick={() => commit.mutate()}>确认提交</Button></DialogFooter>
          </DialogContent>
        </Dialog>
      </div>
    </PageFrame>
  )
}
