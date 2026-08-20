import { useEffect, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "react-router-dom"
import { SaveIcon } from "lucide-react"
import { toast } from "sonner"

import { MarkdownEditor } from "@/features/markdown-editor"
import { useUnsavedChanges } from "@/features/unsaved-changes-context"
import { CategoryPicker, TagPicker, type TaxonomySelection } from "@/features/taxonomy-picker"
import { PageFrame, PageTitle } from "@/components/page-frame"
import { Button } from "@/components/ui/button"
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Spinner } from "@/components/ui/spinner"
import { apiGet, apiPost, type Article, type PrivateTaxonomy, queryKeys } from "@/lib/api"

type CreateArticleResponse = {
  operation_id: string
  article: Article
  created_category: boolean
  replayed: boolean
}

export function NewArticlePage() {
  const navigate = useNavigate()
  const client = useQueryClient()
  const { allowNavigation, setDirty } = useUnsavedChanges()
  const taxonomy = useQuery({ queryKey: queryKeys.taxonomy, queryFn: () => apiGet<PrivateTaxonomy>("/api/taxonomy") })
  const [title, setTitle] = useState("")
  const [markdown, setMarkdown] = useState("")
  const [category, setCategory] = useState<TaxonomySelection>({ kind: "inbox", name: "暂不分类" })
  const [tags, setTags] = useState<TaxonomySelection[]>([])
  const dirty = Boolean(title || markdown || category.kind !== "inbox" || tags.length)

  useEffect(() => {
    setDirty(dirty)
    return () => setDirty(false)
  }, [dirty, setDirty])

  useEffect(() => {
    const preventUnload = (event: BeforeUnloadEvent) => {
      if (!dirty) return
      event.preventDefault()
    }
    window.addEventListener("beforeunload", preventUnload)
    return () => window.removeEventListener("beforeunload", preventUnload)
  }, [dirty])

  const create = useMutation<CreateArticleResponse, Error>({
    mutationFn: () => apiPost<CreateArticleResponse>("/api/articles", {
      title: title.trim(),
      markdown,
      category: category.kind === "existing"
        ? { kind: "existing", id: category.id }
        : category.kind === "create"
          ? { kind: "create", name: category.name }
          : { kind: "inbox" },
      tags: tags.map((item) => item.name),
    }),
    onSuccess: async (result) => {
      await Promise.all([
        client.invalidateQueries({ queryKey: queryKeys.articles }),
        client.invalidateQueries({ queryKey: queryKeys.categories }),
        client.invalidateQueries({ queryKey: queryKeys.taxonomy }),
      ])
      client.setQueryData(queryKeys.article(result.article.path), result.article)
      toast.success("词条已创建")
      allowNavigation()
      navigate(`/${result.article.path}`)
    },
    onError: (error) => toast.error(error.message),
  })

  const canSubmit = Boolean(title.trim() && markdown.trim()) && !create.isPending
  const cancel = () => navigate("/")

  return (
    <PageFrame>
      <div className="mx-auto max-w-6xl">
        <PageTitle
          eyebrow="私人 Wiki"
          title="新建词条"
          actions={(
            <>
              <Button variant="outline" onClick={cancel}>取消</Button>
              <Button disabled={!canSubmit} onClick={() => create.mutate()}>
                {create.isPending ? <Spinner data-icon="inline-start" /> : <SaveIcon data-icon="inline-start" />}
                保存词条
              </Button>
            </>
          )}
        />

        <FieldGroup className="mb-5">
          <Field data-invalid={Boolean(title && !title.trim())}>
            <FieldLabel htmlFor="article-title">标题</FieldLabel>
            <Input
              id="article-title"
              value={title}
              maxLength={120}
              autoFocus
              placeholder="词条标题"
              onChange={(event) => setTitle(event.target.value)}
              aria-invalid={Boolean(title && !title.trim())}
            />
            <FieldDescription>{title.length}/120</FieldDescription>
            {title && !title.trim() && <FieldError>标题不能为空</FieldError>}
          </Field>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field>
              <FieldLabel>主分类</FieldLabel>
              <CategoryPicker
                options={(taxonomy.data?.categories ?? []).map((item) => ({ id: item.id, name: item.name }))}
                value={category}
                onChange={setCategory}
                allowInbox
              />
            </Field>
            <Field>
              <FieldLabel>标签</FieldLabel>
              <TagPicker
                options={(taxonomy.data?.tags ?? []).map((name) => ({ id: name.normalize("NFKC").toLocaleLowerCase(), name }))}
                value={tags}
                onChange={setTags}
              />
            </Field>
          </div>
          <Field>
            <FieldLabel>Markdown 正文</FieldLabel>
            <MarkdownEditor value={markdown} onChange={setMarkdown} fromPath="" previewTitle={title.trim()} />
            {!markdown.trim() && title.trim() && <FieldDescription>正文尚未填写</FieldDescription>}
          </Field>
        </FieldGroup>

        <span className="sr-only" aria-live="polite">{create.isPending ? "正在保存词条" : ""}</span>
      </div>
    </PageFrame>
  )
}
