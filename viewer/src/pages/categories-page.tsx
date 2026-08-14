import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  ArchiveIcon,
  ArrowDownIcon,
  ArrowUpIcon,
  FolderPlusIcon,
  PencilIcon,
  RotateCcwIcon,
  Trash2Icon,
} from "lucide-react"
import { toast } from "sonner"

import { PageFrame, PageTitle } from "@/components/page-frame"
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { apiGet, apiPost, type Category, queryKeys } from "@/lib/api"

export function CategoriesPage() {
  const client = useQueryClient()
  const [dialog, setDialog] = useState<{
    action: string
    category?: Category
  } | null>(null)
  const [name, setName] = useState("")
  const [description, setDescription] = useState("")
  const [sortOrder, setSortOrder] = useState<number | null>(null)
  const [preview, setPreview] = useState<{
    preview_id: string
    conflicts: { kind: string }[]
    can_commit: boolean
    affected_articles: string[]
  } | null>(null)
  const categories = useQuery({
    queryKey: [...queryKeys.categories, "all"],
    queryFn: () => apiGet<Category[]>("/api/categories?status=all"),
  })
  const makePreview = useMutation({
    mutationFn: () =>
      apiPost<typeof preview>(
        "/api/categories/preview",
        {
          action: dialog?.action,
          category_id: dialog?.category?.category_id ?? "",
          name,
          description,
          sort_order: sortOrder,
        },
        false
      ),
    onSuccess: setPreview,
    onError: (error) => toast.error(error.message),
  })
  const commit = useMutation({
    mutationFn: () =>
      apiPost<{ operation_id: string }>("/api/categories/commit", {
        preview_id: preview?.preview_id,
      }),
    onSuccess: async (result) => {
      toast.success(`分类操作完成 · ${result.operation_id}`)
      setDialog(null)
      setPreview(null)
      await client.invalidateQueries()
    },
    onError: (error) => toast.error(error.message),
  })
  function open(action: string, category?: Category) {
    setDialog({ action, category })
    setName(category?.name ?? "")
    setDescription(category?.description ?? "")
    setSortOrder(category?.sort_order ?? null)
    setPreview(null)
  }
  const list = (status: string) => (
    <div className="divide-y border-y">
      {categories.data
        ?.filter((item) => item.status === status)
        .map((category) => (
          <div
            key={category.category_id}
            className="flex flex-col gap-4 py-5 sm:flex-row sm:items-center sm:justify-between"
          >
            <div>
              <h2 className="font-medium">{category.name}</h2>
              <p className="mt-1 text-sm text-muted-foreground">
                {category.description || "未填写说明"} ·{" "}
                {category.article_count} 篇 ·{" "}
                <code>{category.directory_name}</code>
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              {status === "active" ? (
                <>
                  <Button
                    size="icon-sm"
                    variant="ghost"
                    aria-label={`上移 ${category.name}`}
                    disabled={category.sort_order <= 0}
                    onClick={() => {
                      open("reorder", category)
                      setSortOrder(Math.max(0, category.sort_order - 1))
                    }}
                  >
                    <ArrowUpIcon />
                  </Button>
                  <Button
                    size="icon-sm"
                    variant="ghost"
                    aria-label={`下移 ${category.name}`}
                    onClick={() => {
                      open("reorder", category)
                      setSortOrder(category.sort_order + 1)
                    }}
                  >
                    <ArrowDownIcon />
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => open("rename", category)}
                  >
                    <PencilIcon data-icon="inline-start" />
                    重命名
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => open("archive", category)}
                  >
                    <ArchiveIcon data-icon="inline-start" />
                    归档
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={category.article_count > 0}
                    onClick={() => open("delete", category)}
                  >
                    <Trash2Icon data-icon="inline-start" />
                    删除
                  </Button>
                </>
              ) : (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => open("restore", category)}
                >
                  <RotateCcwIcon data-icon="inline-start" />
                  恢复
                </Button>
              )}
            </div>
          </div>
        ))}
    </div>
  )
  return (
    <PageFrame>
      <div className="mx-auto max-w-5xl">
        <PageTitle
          title="分类管理"
          description="分类是当前私有空间的一层真实目录。重命名、归档和删除都必须先预览再提交。"
          actions={
            <Button onClick={() => open("create")}>
              <FolderPlusIcon data-icon="inline-start" />
              新建分类
            </Button>
          }
        />
        <Tabs defaultValue="active">
          <TabsList>
            <TabsTrigger value="active">使用中</TabsTrigger>
            <TabsTrigger value="archived">已归档</TabsTrigger>
          </TabsList>
          <TabsContent value="active" className="mt-6">
            {list("active")}
          </TabsContent>
          <TabsContent value="archived" className="mt-6">
            {list("archived")}
          </TabsContent>
        </Tabs>
        <Dialog
          open={Boolean(dialog)}
          onOpenChange={(value) => !value && setDialog(null)}
        >
          <DialogContent>
            <DialogHeader>
              <DialogTitle>
                {dialog?.action === "create"
                  ? "新建分类"
                  : dialog?.action === "rename"
                    ? "重命名分类"
                    : dialog?.action === "archive"
                      ? "归档分类"
                      : dialog?.action === "restore"
                        ? "恢复分类"
                        : dialog?.action === "reorder"
                          ? "调整分类顺序"
                          : "删除空分类"}
              </DialogTitle>
              <DialogDescription>
                实际磁盘变更会记录 operation ID，并支持回滚。
              </DialogDescription>
            </DialogHeader>
            {["create", "rename"].includes(dialog?.action ?? "") && (
              <div className="flex flex-col gap-3">
                <Input
                  aria-label="分类名称"
                  placeholder="分类名称"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                />
                <Input
                  aria-label="分类说明"
                  placeholder="分类用途说明"
                  value={description}
                  onChange={(event) => setDescription(event.target.value)}
                />
              </div>
            )}
            {preview &&
              (preview.conflicts.length ? (
                <Alert variant="destructive">
                  <AlertTitle>不能提交</AlertTitle>
                  <AlertDescription>
                    {preview.conflicts.map((item) => item.kind).join("、")}
                  </AlertDescription>
                </Alert>
              ) : (
                <Alert>
                  <AlertTitle>预览已就绪</AlertTitle>
                  <AlertDescription>
                    影响 {preview.affected_articles.length} 篇词条。
                  </AlertDescription>
                </Alert>
              ))}
            <DialogFooter>
              <Button variant="outline" onClick={() => setDialog(null)}>
                取消
              </Button>
              {preview ? (
                <Button
                  disabled={!preview.can_commit || commit.isPending}
                  onClick={() => commit.mutate()}
                >
                  确认提交
                </Button>
              ) : (
                <Button
                  disabled={makePreview.isPending}
                  onClick={() => makePreview.mutate()}
                >
                  生成预览
                </Button>
              )}
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </div>
    </PageFrame>
  )
}
