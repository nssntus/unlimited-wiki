import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "react-router-dom"
import { SaveIcon } from "lucide-react"
import { toast } from "sonner"

import { apiGet, apiPost, type Article, type Category, queryKeys } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"

const statuses = ["词条", "草稿", "过时", "有争议"]

export function GovernanceDialog({ article, open, onOpenChange }: { article: Article; open: boolean; onOpenChange: (open: boolean) => void }) {
  const [category, setCategory] = useState(article.category)
  const [status, setStatus] = useState(article.content_status)
  const client = useQueryClient()
  const navigate = useNavigate()
  const categories = useQuery({ queryKey: queryKeys.categories, queryFn: () => apiGet<Category[]>("/api/categories") })
  const save = useMutation({
    mutationFn: () => apiPost<{ article: Article }>("/api/meta", { path: article.path, category, status }),
    onSuccess: ({ article: updated }) => {
      void client.invalidateQueries({ queryKey: queryKeys.articles })
      void client.invalidateQueries({ queryKey: queryKeys.article(article.path) })
      toast.success("治理信息已保存")
      onOpenChange(false)
      navigate(`/${updated.path}`)
    },
    onError: (error) => toast.error(error.message),
  })
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader><DialogTitle>治理词条</DialogTitle><DialogDescription>内容状态由人判断；结构完整度按正文实时计算。</DialogDescription></DialogHeader>
        <FieldGroup>
          <Field><FieldLabel>分类</FieldLabel><Select value={category} onValueChange={(value) => value && setCategory(value)}><SelectTrigger className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{categories.data?.map((item) => <SelectItem key={item.id} value={item.id}>{item.label}</SelectItem>)}</SelectGroup></SelectContent></Select></Field>
          <Field><FieldLabel>内容状态</FieldLabel><Select value={status} onValueChange={(value) => value && setStatus(value)}><SelectTrigger className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{statuses.map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}</SelectGroup></SelectContent></Select></Field>
        </FieldGroup>
        <DialogFooter><Button variant="outline" onClick={() => onOpenChange(false)}>取消</Button><Button disabled={save.isPending} onClick={() => save.mutate()}>{save.isPending ? <Spinner data-icon="inline-start" /> : <SaveIcon data-icon="inline-start" />}保存</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
