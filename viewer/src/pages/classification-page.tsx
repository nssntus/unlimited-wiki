import { useEffect, useMemo, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  CheckCheckIcon,
  EyeIcon,
  FolderPlusIcon,
  Undo2Icon,
} from "lucide-react"
import { toast } from "sonner"

import { PageFrame, PageTitle } from "@/components/page-frame"
import { StatusBadge } from "@/components/markdown-content"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
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
import {
  apiGet,
  apiPost,
  type Article,
  type Category,
  queryKeys,
} from "@/lib/api"

type Candidate = { category_id: string; confidence: number; reason: string }
type WorkItem = {
  article: Article
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
type Workbench = {
  counts: Record<string, number>
  high_confidence_threshold: number
  draft: { revision: number; selections: Selection[] }
  items: WorkItem[]
}
type Selection = {
  article_id: string
  article_revision: string
  decision: string
  category_id?: string
  new_category?: { client_ref: string; name: string; description: string }
  tags: string[]
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
  const draftHydrated = useRef(false)
  const savedDraftPayload = useRef("")
  const [preview, setPreview] = useState<{
    preview_id: string
    moves: { source_path: string; target_path: string }[]
    creates: Category[]
    conflicts: { kind: string }[]
    can_commit: boolean
  } | null>(null)
  const selectedCount = Object.keys(selected).length
  const categoryNames = useMemo(
    () =>
      new Map(categories.data?.map((item) => [item.category_id, item.name])),
    [categories.data]
  )

  const previewMutation = useMutation({
    mutationFn: () =>
      apiPost<typeof preview>(
        "/api/classifications/preview",
        { selections: Object.values(selected) },
        false
      ),
    onSuccess: (value) => setPreview(value),
    onError: (error) => toast.error(error.message),
  })
  const commit = useMutation({
    mutationFn: () =>
      apiPost<{ operation_id: string }>("/api/classifications/commit", {
        preview_id: preview?.preview_id,
      }),
    onSuccess: async (result) => {
      toast.success(`归类完成 · ${result.operation_id}`)
      setPreview(null)
      savedDraftPayload.current = "[]"
      setDraftRevision(0)
      setSelected({})
      await client.invalidateQueries()
    },
    onError: (error) => toast.error(error.message),
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

  useEffect(() => {
    if (!workbench.data || draftHydrated.current) return
    setSelected(
      Object.fromEntries(
        workbench.data.draft.selections.map((item) => [item.article_id, item])
      )
    )
    setDraftRevision(workbench.data.draft.revision)
    savedDraftPayload.current = JSON.stringify(workbench.data.draft.selections)
    draftHydrated.current = true
  }, [workbench.data])

  useEffect(() => {
    if (!draftHydrated.current) return
    const selections = Object.values(selected)
    const serialized = JSON.stringify(selections)
    if (serialized === savedDraftPayload.current) return
    const timer = window.setTimeout(() => {
      void apiPost<{ revision: number }>(
        "/api/classifications/draft",
        {
          selections,
          expected_revision: draftRevision,
        },
        false
      )
        .then((result) => {
          savedDraftPayload.current = serialized
          setDraftRevision(result.revision)
        })
        .catch((error: Error) => toast.error(error.message))
    }, 400)
    return () => window.clearTimeout(timer)
  }, [draftRevision, selected])

  function choose(item: WorkItem, categoryId: string) {
    const tags = item.suggestion?.suggestion?.tags ?? item.article.tags
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
    const next = { ...selected }
    for (const item of workbench.data?.items ?? []) {
      const candidate = item.suggestion?.suggestion?.candidates?.[0]
      if (candidate && candidate.confidence >= 0.85)
        next[item.article.article_id] = {
          article_id: item.article.article_id,
          article_revision: item.article.revision,
          decision: "existing",
          category_id: candidate.category_id,
          tags: item.suggestion?.suggestion?.tags ?? [],
        }
    }
    setSelected(next)
  }

  if (workbench.isLoading)
    return (
      <PageFrame>
        <Skeleton className="h-96" />
      </PageFrame>
    )
  return (
    <PageFrame>
      <div className="mx-auto max-w-6xl">
        <PageTitle
          title="待归类"
          description="AI 基于完整正文给出建议；选择只保存在当前工作台，预览并确认后才会创建目录和移动文件。"
          actions={
            <Button variant="outline" onClick={selectHighConfidence}>
              <CheckCheckIcon data-icon="inline-start" />
              选择 ≥85% 推荐
            </Button>
          }
        />
        {!workbench.data?.items.length ? (
          <Alert>
            <AlertTitle>没有待归类词条</AlertTitle>
            <AlertDescription>新生成的正本会先进入这里。</AlertDescription>
          </Alert>
        ) : (
          <div className="divide-y border-y">
            {workbench.data?.items.map((item) => {
              const suggestion = item.suggestion?.suggestion
              const current = selected[item.article.article_id]
              return (
                <section
                  key={item.article.article_id}
                  className="grid gap-4 py-5 lg:grid-cols-[minmax(0,1fr)_20rem]"
                >
                  <div>
                    <div className="flex flex-wrap items-center gap-2">
                      <h2 className="font-medium">{item.article.title}</h2>
                      <StatusBadge
                        value={
                          item.group === "high_confidence"
                            ? "高置信"
                            : item.group === "failed"
                              ? "建议失败"
                              : "需确认"
                        }
                        kind={
                          item.group === "high_confidence" ? "good" : "warn"
                        }
                      />
                    </div>
                    <code className="mt-1 block text-xs break-all text-muted-foreground">
                      {item.article.path}
                    </code>
                    {suggestion?.candidates?.map((candidate) => (
                      <div key={candidate.category_id} className="mt-3 text-sm">
                        <span className="font-medium">
                          {categoryNames.get(candidate.category_id) ??
                            candidate.category_id}{" "}
                          · {Math.round(candidate.confidence * 100)}%
                        </span>
                        <p className="text-muted-foreground">
                          {candidate.reason}
                        </p>
                      </div>
                    ))}
                    {item.suggestion?.status === "failed" && (
                      <div className="mt-3">
                        <p className="text-sm text-destructive">
                          {item.suggestion.error_type} ·{" "}
                          {item.suggestion.error_message}
                        </p>
                        <Button
                          className="mt-2"
                          size="sm"
                          variant="outline"
                          onClick={() => retry.mutate(item.article.article_id)}
                        >
                          重试 AI 建议
                        </Button>
                      </div>
                    )}
                  </div>
                  <div className="flex flex-col gap-3">
                    <Select
                      value={current?.category_id ?? ""}
                      onValueChange={(value) => value && choose(item, value)}
                    >
                      <SelectTrigger className="w-full">
                        <SelectValue placeholder="选择现有分类" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectGroup>
                          {categories.data?.map((category) => (
                            <SelectItem
                              key={category.category_id}
                              value={category.category_id}
                            >
                              {category.name}
                            </SelectItem>
                          ))}
                        </SelectGroup>
                      </SelectContent>
                    </Select>
                    {suggestion?.new_category && (
                      <Button
                        variant="outline"
                        onClick={() =>
                          setSelected((currentSelections) => ({
                            ...currentSelections,
                            [item.article.article_id]: {
                              article_id: item.article.article_id,
                              article_revision: item.article.revision,
                              decision: "new",
                              new_category: {
                                client_ref: item.article.article_id,
                                name: suggestion.new_category!.name,
                                description:
                                  suggestion.new_category!.description,
                              },
                              tags: suggestion.tags ?? [],
                            },
                          }))
                        }
                      >
                        <FolderPlusIcon data-icon="inline-start" />
                        新建 {suggestion.new_category.name}
                      </Button>
                    )}
                    <Input
                      aria-label={`${item.article.title} 标签`}
                      placeholder="标签以分号分隔"
                      value={(current?.tags ?? suggestion?.tags ?? []).join(
                        "; "
                      )}
                      onChange={(event) => {
                        const tags = event.target.value
                          .split(";")
                          .map((value) => value.trim())
                          .filter(Boolean)
                        const base = current ?? {
                          article_id: item.article.article_id,
                          article_revision: item.article.revision,
                          decision: "defer",
                          tags: [],
                        }
                        setSelected((state) => ({
                          ...state,
                          [item.article.article_id]: { ...base, tags },
                        }))
                      }}
                    />
                  </div>
                </section>
              )
            })}
          </div>
        )}
        <div className="sticky bottom-0 mt-6 flex items-center justify-between border-t bg-background/95 py-4 backdrop-blur">
          <span className="text-sm text-muted-foreground">
            已选择 {selectedCount} 项
          </span>
          <Button
            disabled={!selectedCount || previewMutation.isPending}
            onClick={() => previewMutation.mutate()}
          >
            <EyeIcon data-icon="inline-start" />
            预览变更
          </Button>
        </div>
        <Dialog
          open={Boolean(preview)}
          onOpenChange={(open) => !open && setPreview(null)}
        >
          <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-2xl">
            <DialogHeader>
              <DialogTitle>确认批量归类</DialogTitle>
              <DialogDescription>
                提交后将原子移动文件、更新元数据、链接、索引和操作日志。
              </DialogDescription>
            </DialogHeader>
            {preview?.creates.map((item) => (
              <Alert key={item.category_id}>
                <FolderPlusIcon />
                <AlertTitle>创建分类 {item.name}</AlertTitle>
                <AlertDescription>{item.directory_name}</AlertDescription>
              </Alert>
            ))}
            <div className="divide-y">
              {preview?.moves.map((move) => (
                <div key={move.source_path} className="py-3 text-sm">
                  <code className="break-all">{move.source_path}</code>
                  <span className="mx-2">→</span>
                  <code className="break-all">{move.target_path}</code>
                </div>
              ))}
            </div>
            {preview?.conflicts.length ? (
              <Alert variant="destructive">
                <AlertTitle>存在阻断冲突</AlertTitle>
                <AlertDescription>
                  {preview.conflicts.map((item) => item.kind).join("、")}
                </AlertDescription>
              </Alert>
            ) : null}
            <DialogFooter>
              <Button variant="outline" onClick={() => setPreview(null)}>
                <Undo2Icon data-icon="inline-start" />
                返回修改
              </Button>
              <Button
                disabled={!preview?.can_commit || commit.isPending}
                onClick={() => commit.mutate()}
              >
                确认提交
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </div>
    </PageFrame>
  )
}
