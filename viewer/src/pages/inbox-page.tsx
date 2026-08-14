import { useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link, useNavigate } from "react-router-dom"
import { FileInputIcon, InboxIcon, RefreshCwIcon, UploadIcon } from "lucide-react"
import { toast } from "sonner"

import { apiGet, apiPost, type RawInboxItem, queryKeys } from "@/lib/api"
import { StatusBadge } from "@/components/markdown-content"
import { PageFrame, PageTitle } from "@/components/page-frame"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"

type UploadResult = { created: boolean; raw: RawInboxItem }
const allowedExtensions = [".md", ".markdown", ".txt"]

export function InboxPage() {
  const navigate = useNavigate()
  const client = useQueryClient()
  const inputRef = useRef<HTMLInputElement>(null)
  const [dragging, setDragging] = useState(false)
  const inbox = useQuery({ queryKey: queryKeys.inbox, queryFn: () => apiGet<RawInboxItem[]>("/api/ingest") })
  const upload = useMutation({
    mutationFn: async (file: File) => {
      if (!allowedExtensions.some((extension) => file.name.toLowerCase().endsWith(extension))) throw new Error("仅支持 .md、.markdown 和 .txt 文件")
      if (file.size > 10 * 1024 * 1024) throw new Error("文件不能超过 10 MiB")
      const content = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer())
      return apiPost<UploadResult>("/api/ingest/upload", { filename: file.name, content })
    },
    onSuccess: (result) => {
      void client.invalidateQueries({ queryKey: queryKeys.inbox })
      toast.success(result.created ? "原材料已加入原料箱" : "相同原材料已在原料箱中")
      void navigate(`/ingest/${result.raw.path}`)
    },
    onError: (error) => toast.error(error instanceof TypeError ? "文件不是有效的 UTF-8 文本" : error.message),
  })
  const acceptFile = (file?: File) => {
    if (file && !upload.isPending) upload.mutate(file)
  }

  return <PageFrame><div className="mx-auto max-w-5xl"><PageTitle eyebrow="治理 / Raw" title="原料箱" description="从电脑添加 Markdown 或文本原材料；确认摄入前只保存 Raw，原文不会被修改。" actions={<div className="flex flex-wrap gap-2"><input ref={inputRef} className="sr-only" type="file" accept=".md,.markdown,.txt,text/markdown,text/plain" onChange={(event) => { acceptFile(event.target.files?.[0]); event.currentTarget.value = "" }} /><Button size="sm" disabled={upload.isPending} onClick={() => inputRef.current?.click()}>{upload.isPending ? <Spinner data-icon="inline-start" /> : <UploadIcon data-icon="inline-start" />}添加原材料</Button><Button variant="outline" size="sm" onClick={() => void inbox.refetch()}><RefreshCwIcon data-icon="inline-start" />重新扫描</Button></div>} />
    <Alert className="mb-6" onDragEnter={(event) => { event.preventDefault(); setDragging(true) }} onDragOver={(event) => event.preventDefault()} onDragLeave={() => setDragging(false)} onDrop={(event) => { event.preventDefault(); setDragging(false); acceptFile(event.dataTransfer.files[0]) }} data-dragging={dragging || undefined}>
      <UploadIcon />
      <AlertTitle>{dragging ? "松开以添加原材料" : "也可以把文件拖到这里"}</AlertTitle>
      <AlertDescription>支持 UTF-8 编码的 .md、.markdown、.txt，单个文件不超过 10 MiB。添加后会进入标题、分类和处置确认。</AlertDescription>
    </Alert>
    {inbox.isLoading ? <div className="flex flex-col gap-3"><Skeleton className="h-12 w-full" /><Skeleton className="h-12 w-full" /></div> : inbox.data?.length ? <div className="overflow-x-auto rounded-md border"><Table><TableHeader><TableRow><TableHead>文件</TableHead><TableHead>状态</TableHead><TableHead>大小</TableHead><TableHead className="text-right">操作</TableHead></TableRow></TableHeader><TableBody>{inbox.data.map((item) => <TableRow key={item.path}><TableCell><div className="font-medium">{item.title || item.path.split("/").pop()}</div><code className="break-all text-xs text-muted-foreground">{item.path}</code></TableCell><TableCell><StatusBadge value={item.status} kind={item.status === "unlinked" ? "warn" : item.status === "integrity_changed" ? "bad" : "neutral"} /></TableCell><TableCell>{Math.ceil(item.size / 1024)} KiB</TableCell><TableCell className="text-right"><Button size="sm" variant="outline" disabled={!item.ingestable} render={<Link to={`/ingest/${item.path}`} />}><FileInputIcon data-icon="inline-start" />预览</Button></TableCell></TableRow>)}</TableBody></Table></div> : <Empty className="min-h-80"><EmptyHeader><EmptyMedia variant="icon"><InboxIcon /></EmptyMedia><EmptyTitle>没有待处理 Raw</EmptyTitle><EmptyDescription>点击“添加原材料”，或把文件拖入上方区域。</EmptyDescription></EmptyHeader></Empty>}
  </div></PageFrame>
}
