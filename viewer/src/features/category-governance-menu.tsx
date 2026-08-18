import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { ArchiveIcon, EllipsisIcon, PencilIcon, RotateCcwIcon } from "lucide-react"
import { toast } from "sonner"

import { apiPost, queryKeys, type Category } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"

type Action = "rename" | "archive" | "restore"
type Preview = { preview_id: string; can_commit: boolean; affected_articles: string[]; conflicts: Array<{ kind: string }> }

export function CategoryGovernanceMenu({ category }: { category: Category }) {
  const client = useQueryClient()
  const [action, setAction] = useState<Action | null>(null)
  const [name, setName] = useState(category.name)
  const [description, setDescription] = useState(category.description)
  const [preview, setPreview] = useState<Preview | null>(null)
  const close = () => { setAction(null); setPreview(null) }
  const open = (next: Action) => {
    setName(category.name)
    setDescription(category.description)
    setPreview(null)
    setAction(next)
  }
  const makePreview = useMutation({
    mutationFn: () => apiPost<Preview>("/api/categories/preview", {
      action,
      category_id: category.category_id,
      name,
      description,
    }, false),
    onSuccess: setPreview,
    onError: (error) => toast.error(error.message),
  })
  const commit = useMutation({
    mutationFn: () => apiPost<{ operation_id: string }>("/api/categories/commit", { preview_id: preview?.preview_id }),
    onSuccess: async (result) => {
      await Promise.all([
        client.invalidateQueries({ queryKey: queryKeys.categories }),
        client.invalidateQueries({ queryKey: queryKeys.taxonomy }),
        client.invalidateQueries({ queryKey: queryKeys.articles }),
      ])
      toast.success(`分类操作已提交 · ${result.operation_id}`)
      close()
    },
    onError: (error) => toast.error(error.message),
  })
  return <>
    <DropdownMenu><DropdownMenuTrigger render={<Button type="button" size="icon-sm" variant="ghost" aria-label={`管理分类 ${category.name}`} />}><EllipsisIcon /></DropdownMenuTrigger><DropdownMenuContent align="end"><DropdownMenuGroup>{category.status === "active" ? <><DropdownMenuItem onClick={() => open("rename")}><PencilIcon />重命名</DropdownMenuItem><DropdownMenuItem onClick={() => open("archive")}><ArchiveIcon />归档</DropdownMenuItem></> : <DropdownMenuItem onClick={() => open("restore")}><RotateCcwIcon />恢复</DropdownMenuItem>}</DropdownMenuGroup></DropdownMenuContent></DropdownMenu>
    <Dialog open={Boolean(action)} onOpenChange={(value) => !value && close()}><DialogContent><DialogHeader><DialogTitle>{action === "rename" ? "重命名分类" : action === "archive" ? "归档分类" : "恢复分类"}</DialogTitle><DialogDescription>先预览再提交。分类目录、正本元数据、索引和操作记录会作为一个文件事务更新。</DialogDescription></DialogHeader>{action === "rename" && <FieldGroup><Field><FieldLabel htmlFor="category-name">分类名称</FieldLabel><Input id="category-name" value={name} onChange={(event) => { setName(event.target.value); setPreview(null) }} /></Field><Field><FieldLabel htmlFor="category-description">说明</FieldLabel><Input id="category-description" value={description} onChange={(event) => { setDescription(event.target.value); setPreview(null) }} /></Field></FieldGroup>}{preview && <div className="border-y py-4 text-sm"><p>影响 {preview.affected_articles.length} 篇词条。</p>{preview.conflicts.length > 0 && <p className="mt-2 text-destructive">检测到冲突，当前不可提交。</p>}</div>}<DialogFooter><Button type="button" variant="outline" onClick={close}>取消</Button>{preview ? <Button disabled={!preview.can_commit || commit.isPending} onClick={() => commit.mutate()}>确认提交</Button> : <Button disabled={makePreview.isPending || (action === "rename" && !name.trim())} onClick={() => makePreview.mutate()}>预览变更</Button>}</DialogFooter></DialogContent></Dialog>
  </>
}
