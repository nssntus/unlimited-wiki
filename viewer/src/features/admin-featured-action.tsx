import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { StarIcon } from "lucide-react"
import { toast } from "sonner"

import { apiPost, queryKeys, type AdminPublicEntry } from "@/lib/api"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Spinner } from "@/components/ui/spinner"
import { Textarea } from "@/components/ui/textarea"

export function AdminFeaturedAction({
  entry,
  onClose,
}: {
  entry: AdminPublicEntry | null
  onClose: () => void
}) {
  const client = useQueryClient()
  const [reason, setReason] = useState("")
  const [sortOrder, setSortOrder] = useState(
    String(entry?.featured_order ?? 0)
  )
  const parsedSortOrder = Number(sortOrder)
  const validSortOrder = /^-?\d+$/.test(sortOrder.trim()) &&
    Number.isSafeInteger(parsedSortOrder)
  const feature = useMutation({
    mutationFn: () =>
      apiPost(`/api/admin/public-entries/${entry!.id}/featured`, {
        featured: !entry!.featured,
        reason,
        sort_order: entry!.featured ? 0 : parsedSortOrder,
      }),
    onSuccess: async () => {
      await client.cancelQueries({ queryKey: queryKeys.square })
      client.removeQueries({ queryKey: queryKeys.square })
      await client.invalidateQueries({
        queryKey: queryKeys.adminPublicEntries("published"),
      })
      toast.success(entry?.featured ? "已取消精选" : "已加入精选")
      setReason("")
      onClose()
    },
    onError: (error) => toast.error(error.message),
  })
  const close = () => {
    if (feature.isPending) return
    setReason("")
    onClose()
  }

  return (
    <Dialog open={Boolean(entry)} onOpenChange={(open) => !open && close()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{entry?.featured ? "取消精选" : "加入精选"}</DialogTitle>
          <DialogDescription>
            {entry?.snapshot.title} · v{entry?.version}。操作会立即更新广场首页，理由将写入审计记录。
          </DialogDescription>
        </DialogHeader>
        {!entry?.featured && (
          <Field>
            <FieldLabel htmlFor="featured-sort-order">精选顺序</FieldLabel>
            <Input
              id="featured-sort-order"
              type="number"
              step="1"
              value={sortOrder}
              aria-invalid={!validSortOrder}
              aria-describedby={
                !validSortOrder ? "featured-sort-order-error" : undefined
              }
              onChange={(event) => setSortOrder(event.target.value)}
            />
            {!validSortOrder && (
              <p
                id="featured-sort-order-error"
                className="text-sm text-destructive"
                role="alert"
              >
                请输入有效整数
              </p>
            )}
          </Field>
        )}
        <Field>
          <FieldLabel htmlFor="featured-reason">策展理由</FieldLabel>
          <Textarea
            id="featured-reason"
            value={reason}
            autoFocus
            placeholder={
              entry?.featured
                ? "说明取消精选的原因"
                : "说明为什么推荐这篇词条"
            }
            onChange={(event) => setReason(event.target.value)}
          />
        </Field>
        <div className="flex flex-wrap justify-end gap-2">
          <Button variant="outline" disabled={feature.isPending} onClick={close}>
            取消
          </Button>
          <Button
            disabled={
              !reason.trim() || feature.isPending ||
              (!entry?.featured && !validSortOrder)
            }
            onClick={() => feature.mutate()}
          >
            {feature.isPending ? (
              <Spinner data-icon="inline-start" />
            ) : (
              <StarIcon data-icon="inline-start" />
            )}
            {entry?.featured ? "确认取消精选" : "确认加入精选"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
