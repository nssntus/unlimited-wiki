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
const allowedExtensions = [
  ".md", ".markdown", ".txt", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml", ".html", ".htm",
  ".pdf", ".epub", ".doc", ".docx", ".docm", ".odt", ".rtf",
  ".xls", ".xlsx", ".xlsm", ".ods", ".ppt", ".pptx", ".pptm", ".odp",
  ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".gif", ".heic",
]
const fileAccept = allowedExtensions.join(",")

function fileToBase64(file: File) {
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(new Error("无法读取文件"))
    reader.onload = () => {
      const value = String(reader.result || "")
      const separator = value.indexOf(",")
      if (separator < 0) reject(new Error("无法读取文件"))
      else resolve(value.slice(separator + 1))
    }
    reader.readAsDataURL(file)
  })
}

export function InboxPage() {
  const navigate = useNavigate()
  const client = useQueryClient()
  const inputRef = useRef<HTMLInputElement>(null)
  const [dragging, setDragging] = useState(false)
  const inbox = useQuery({ queryKey: queryKeys.inbox, queryFn: () => apiGet<RawInboxItem[]>("/api/ingest") })
  const upload = useMutation({
    mutationFn: async (file: File) => {
      if (!allowedExtensions.some((extension) => file.name.toLowerCase().endsWith(extension))) throw new Error("暂不支持该文件格式")
      if (file.size > 10 * 1024 * 1024) throw new Error("文件不能超过 10 MiB")
      return apiPost<UploadResult>("/api/ingest/upload", { filename: file.name, content_base64: await fileToBase64(file) })
    },
    onSuccess: (result) => {
      void client.invalidateQueries({ queryKey: queryKeys.inbox })
      toast.success(result.created ? "原材料已加入原料箱" : "相同原材料已在原料箱中")
      void navigate(`/ingest/${result.raw.path}`)
    },
    onError: (error) => {
      const message = error.message
      if (message.includes("图片中未识别到文字")) toast.warning(message, { duration: 6000 })
      else toast.error(message)
    },
  })
  const acceptFile = (file?: File) => {
    if (file && !upload.isPending) upload.mutate(file)
  }

  return <PageFrame><div className="mx-auto max-w-5xl"><PageTitle eyebrow="治理 / Raw" title="原料箱" description="从电脑添加文档或图片原材料；确认摄入前只保存不可变 Raw，解析与 OCR 均在本机完成。" actions={<div className="flex flex-wrap gap-2"><input ref={inputRef} className="sr-only" type="file" accept={fileAccept} onChange={(event) => { acceptFile(event.target.files?.[0]); event.currentTarget.value = "" }} /><Button size="sm" disabled={upload.isPending} onClick={() => inputRef.current?.click()}>{upload.isPending ? <Spinner data-icon="inline-start" /> : <UploadIcon data-icon="inline-start" />}添加原材料</Button><Button variant="outline" size="sm" onClick={() => void inbox.refetch()}><RefreshCwIcon data-icon="inline-start" />重新扫描</Button></div>} />
    <Alert className="mb-6" onDragEnter={(event) => { event.preventDefault(); setDragging(true) }} onDragOver={(event) => event.preventDefault()} onDragLeave={() => setDragging(false)} onDrop={(event) => { event.preventDefault(); setDragging(false); acceptFile(event.dataTransfer.files[0]) }} data-dragging={dragging || undefined}>
      <UploadIcon />
      <AlertTitle>{dragging ? "松开以添加原材料" : "也可以把文件拖到这里"}</AlertTitle>
      <AlertDescription>支持 PDF、Word、Excel、PowerPoint、OpenDocument、常用文本/网页/电子书格式及 PNG、JPEG、WebP、TIFF、BMP、GIF、HEIC。单文件不超过 10 MiB；图片无可识别文字时会退回。</AlertDescription>
    </Alert>
    {inbox.isLoading ? <div className="flex flex-col gap-3"><Skeleton className="h-12 w-full" /><Skeleton className="h-12 w-full" /></div> : inbox.data?.length ? <div className="overflow-x-auto rounded-md border"><Table><TableHeader><TableRow><TableHead>文件</TableHead><TableHead>格式</TableHead><TableHead>状态</TableHead><TableHead>大小</TableHead><TableHead className="text-right">操作</TableHead></TableRow></TableHeader><TableBody>{inbox.data.map((item) => <TableRow key={item.path}><TableCell><div className="font-medium">{item.title || item.path.split("/").pop()}</div><code className="break-all text-xs text-muted-foreground">{item.path}</code>{item.reason ? <div className="mt-1 text-xs text-destructive">{item.reason}</div> : null}</TableCell><TableCell><div className="text-sm">{item.source_format || "未知"}</div>{item.used_ocr ? <div className="text-xs text-muted-foreground">OCR · {item.extracted_chars || 0} 字符</div> : null}</TableCell><TableCell><StatusBadge value={item.status} kind={item.status === "unlinked" ? "warn" : item.status === "integrity_changed" || item.status === "rejected" ? "bad" : "neutral"} /></TableCell><TableCell>{Math.ceil(item.size / 1024)} KiB</TableCell><TableCell className="text-right"><Button size="sm" variant="outline" disabled={!item.ingestable} render={<Link to={`/ingest/${item.path}`} />}><FileInputIcon data-icon="inline-start" />预览</Button></TableCell></TableRow>)}</TableBody></Table></div> : <Empty className="min-h-80"><EmptyHeader><EmptyMedia variant="icon"><InboxIcon /></EmptyMedia><EmptyTitle>没有待处理 Raw</EmptyTitle><EmptyDescription>点击“添加原材料”，或把文件拖入上方区域。</EmptyDescription></EmptyHeader></Empty>}
  </div></PageFrame>
}
