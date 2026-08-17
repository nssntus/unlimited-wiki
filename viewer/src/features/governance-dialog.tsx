import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "react-router-dom"
import { SaveIcon } from "lucide-react"
import { toast } from "sonner"

import { apiGet, apiPost, type Article, type PrivateTaxonomy, queryKeys } from "@/lib/api"
import { CategoryPicker, TagPicker, type TaxonomySelection } from "@/features/taxonomy-picker"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"

const statuses = ["词条", "草稿", "过时", "有争议"]

export function GovernanceDialog({ article, open, onOpenChange }: { article: Article; open: boolean; onOpenChange: (open: boolean) => void }) {
  const [category, setCategory] = useState<TaxonomySelection>(article.primary_category_id ? { kind: "existing", id: article.primary_category_id, name: article.category_label } : { kind: "inbox", name: "暂不分类" })
  const [tags, setTags] = useState<TaxonomySelection[]>(article.tags.map((name) => ({ kind: "existing", id: name.normalize("NFKC").toLocaleLowerCase(), name })))
  const [status, setStatus] = useState(article.content_status)
  const client = useQueryClient()
  const navigate = useNavigate()
  const taxonomy = useQuery({ queryKey: queryKeys.taxonomy, queryFn: () => apiGet<PrivateTaxonomy>("/api/taxonomy") })
  const save = useMutation({
    mutationFn: () => apiPost<{ article: Article }>("/api/article/taxonomy", {
      path: article.path,
      revision: article.revision,
      category: category.kind === "existing" ? { kind: "existing", id: category.id } : category.kind === "inbox" ? { kind: "inbox" } : { kind: "create", name: category.name },
      tags: tags.map((tag) => tag.name),
      status,
    }),
    onSuccess: async ({ article: updated }) => {
      await Promise.all([
        client.invalidateQueries({ queryKey: queryKeys.articles }),
        client.invalidateQueries({ queryKey: queryKeys.categories }),
        client.invalidateQueries({ queryKey: queryKeys.taxonomy }),
        client.invalidateQueries({ queryKey: queryKeys.article(article.path) }),
      ])
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
          <Field><FieldLabel>主分类</FieldLabel><CategoryPicker options={(taxonomy.data?.categories ?? []).map((item) => ({ id: item.id, name: item.name }))} value={category} onChange={setCategory} allowInbox /></Field>
          <Field><FieldLabel>标签</FieldLabel><TagPicker options={(taxonomy.data?.tags ?? []).map((name) => ({ id: name.normalize("NFKC").toLocaleLowerCase(), name }))} value={tags} onChange={setTags} /></Field>
          <Field><FieldLabel>内容状态</FieldLabel><Select value={status} onValueChange={(value) => value && setStatus(value)}><SelectTrigger className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{statuses.map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}</SelectGroup></SelectContent></Select></Field>
        </FieldGroup>
        <DialogFooter><Button variant="outline" onClick={() => onOpenChange(false)}>取消</Button><Button disabled={save.isPending} onClick={() => save.mutate()}>{save.isPending ? <Spinner data-icon="inline-start" /> : <SaveIcon data-icon="inline-start" />}保存</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
