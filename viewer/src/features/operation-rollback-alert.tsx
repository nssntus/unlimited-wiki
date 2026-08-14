import { useMutation, useQueryClient } from "@tanstack/react-query"
import { RotateCcwIcon } from "lucide-react"
import { toast } from "sonner"

import { InlinePath } from "@/components/markdown-content"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import { apiPost } from "@/lib/api"

export function OperationRollbackAlert({
  operationId,
  label,
  onRolledBack,
}: {
  operationId: string | null
  label: string
  onRolledBack: () => void
}) {
  const client = useQueryClient()
  const rollback = useMutation({
    mutationFn: () => apiPost(`/api/operations/${operationId}/rollback`, {}),
    onSuccess: async () => {
      await client.invalidateQueries()
      toast.success(`${label}已回滚`)
      onRolledBack()
    },
    onError: (error) => toast.error(error.message),
  })
  if (!operationId) return null
  return (
    <Alert className="mb-6">
      <RotateCcwIcon />
      <AlertTitle>{label}可回滚</AlertTitle>
      <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
        <span>
          Operation <InlinePath>{operationId}</InlinePath>
        </span>
        <AlertDialog>
          <AlertDialogTrigger render={<Button size="sm" variant="outline" />}>
            回滚
          </AlertDialogTrigger>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>回滚本次{label}？</AlertDialogTitle>
              <AlertDialogDescription>
                仅当受影响文件在提交后没有继续修改时才能回滚。
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>取消</AlertDialogCancel>
              <AlertDialogAction
                disabled={rollback.isPending}
                onClick={() => rollback.mutate()}
              >
                确认回滚
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </AlertDescription>
    </Alert>
  )
}
