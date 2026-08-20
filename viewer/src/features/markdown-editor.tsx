import { useRef } from "react"
import {
  BoldIcon,
  Code2Icon,
  Heading2Icon,
  ItalicIcon,
  LinkIcon,
  ListIcon,
  ListOrderedIcon,
  QuoteIcon,
} from "lucide-react"

import { MarkdownContent } from "@/components/markdown-content"
import { Button } from "@/components/ui/button"
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from "@/components/ui/resizable"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"

type FormatAction = "heading" | "bold" | "italic" | "link" | "unordered" | "ordered" | "quote" | "code"
type SelectionTransform = {
  replacement: string
  start?: number
  end?: number
  selectStart: number
  selectEnd: number
}

const tools: Array<{ action: FormatAction; label: string; icon: typeof BoldIcon }> = [
  { action: "heading", label: "二级标题", icon: Heading2Icon },
  { action: "bold", label: "粗体", icon: BoldIcon },
  { action: "italic", label: "斜体", icon: ItalicIcon },
  { action: "link", label: "链接", icon: LinkIcon },
  { action: "unordered", label: "无序列表", icon: ListIcon },
  { action: "ordered", label: "有序列表", icon: ListOrderedIcon },
  { action: "quote", label: "引用", icon: QuoteIcon },
  { action: "code", label: "代码", icon: Code2Icon },
]

function transformSelection(value: string, start: number, end: number, action: FormatAction): SelectionTransform {
  const selected = value.slice(start, end)
  const inline = (before: string, after: string, placeholder: string) => {
    const content = selected || placeholder
    return { replacement: `${before}${content}${after}`, selectStart: before.length, selectEnd: before.length + content.length }
  }
  if (action === "bold") return inline("**", "**", "粗体文字")
  if (action === "italic") return inline("*", "*", "斜体文字")
  if (action === "link") return inline("[", "](https://)", "链接文字")
  if (action === "code") {
    if (selected.includes("\n")) return inline("```\n", "\n```", "代码")
    return inline("`", "`", "代码")
  }

  const lineStart = value.lastIndexOf("\n", Math.max(0, start - 1)) + 1
  const nextBreak = value.indexOf("\n", end)
  const lineEnd = nextBreak === -1 ? value.length : nextBreak
  const block = value.slice(lineStart, lineEnd) || (action === "heading" ? "标题" : "列表项")
  const lines = block.split("\n")
  const replacement = lines.map((line, index) => {
    if (action === "heading") return `${index === 0 ? "## " : ""}${line}`
    if (action === "quote") return `> ${line}`
    if (action === "ordered") return `${index + 1}. ${line}`
    return `- ${line}`
  }).join("\n")
  return { replacement, start: lineStart, end: lineEnd, selectStart: 0, selectEnd: replacement.length }
}

function EditorToolbar({ onFormat }: { onFormat: (action: FormatAction) => void }) {
  return (
    <div role="toolbar" aria-label="Markdown 格式工具" className="flex min-h-10 flex-wrap items-center gap-1 border-b bg-muted/30 px-2 py-1.5">
      {tools.map(({ action, label, icon: Icon }) => (
        <Tooltip key={action}>
          <TooltipTrigger render={<Button type="button" variant="ghost" size="icon-sm" aria-label={label} onClick={() => onFormat(action)} />}>
            <Icon />
          </TooltipTrigger>
          <TooltipContent>{label}</TooltipContent>
        </Tooltip>
      ))}
    </div>
  )
}

export function MarkdownEditor({
  value,
  onChange,
  fromPath = "",
  minHeightClass = "min-h-[60svh]",
  previewMarkdown,
  previewTitle,
}: {
  value: string
  onChange: (value: string) => void
  fromPath?: string
  minHeightClass?: string
  previewMarkdown?: string
  previewTitle?: string
}) {
  const desktopRef = useRef<HTMLTextAreaElement | null>(null)
  const mobileRef = useRef<HTMLTextAreaElement | null>(null)
  const activeRef = useRef<HTMLTextAreaElement | null>(null)

  const format = (action: FormatAction) => {
    const textarea = activeRef.current
      ?? (desktopRef.current?.getClientRects().length ? desktopRef.current : mobileRef.current)
    if (!textarea) return
    const start = textarea.selectionStart
    const end = textarea.selectionEnd
    const transformed = transformSelection(value, start, end, action)
    const replaceStart = transformed.start ?? start
    const replaceEnd = transformed.end ?? end
    const next = value.slice(0, replaceStart) + transformed.replacement + value.slice(replaceEnd)
    onChange(next)
    requestAnimationFrame(() => {
      textarea.focus()
      textarea.setSelectionRange(
        replaceStart + transformed.selectStart,
        replaceStart + transformed.selectEnd,
      )
    })
  }

  const textareaProps = {
    value,
    onChange: (event: React.ChangeEvent<HTMLTextAreaElement>) => onChange(event.target.value),
    onFocus: (event: React.FocusEvent<HTMLTextAreaElement>) => { activeRef.current = event.currentTarget },
  }

  return (
    <div className="overflow-hidden rounded-md border bg-background">
      <EditorToolbar onFormat={format} />
      <div className="hidden h-[calc(100svh-18rem)] min-h-[32rem] md:block">
        <ResizablePanelGroup orientation="horizontal">
          <ResizablePanel defaultSize={50} minSize={30}>
            <Textarea
              {...textareaProps}
              ref={desktopRef}
              aria-label="Markdown 正文"
              className="h-full resize-none rounded-none border-0 font-mono text-sm focus-visible:ring-0"
            />
          </ResizablePanel>
          <ResizableHandle withHandle />
          <ResizablePanel defaultSize={50} minSize={30}>
            <div className="h-full overflow-y-auto p-8">
              {previewTitle && <h1 className="mb-6 text-3xl font-semibold">{previewTitle}</h1>}
              <MarkdownContent markdown={previewMarkdown ?? value} fromPath={fromPath} />
            </div>
          </ResizablePanel>
        </ResizablePanelGroup>
      </div>
      <Tabs defaultValue="edit" className="md:hidden">
        <TabsList className="m-2 mb-0 w-[calc(100%-1rem)]">
          <TabsTrigger value="edit">编辑</TabsTrigger>
          <TabsTrigger value="preview">预览</TabsTrigger>
        </TabsList>
        <TabsContent value="edit" className="mt-0">
          <Textarea
            {...textareaProps}
            ref={mobileRef}
            aria-label="Markdown 正文"
            className={`${minHeightClass} resize-none rounded-none border-0 font-mono text-sm focus-visible:ring-0`}
          />
        </TabsContent>
        <TabsContent value="preview" className={`${minHeightClass} mt-0 overflow-y-auto p-5`}>
          {previewTitle && <h1 className="mb-5 text-2xl font-semibold">{previewTitle}</h1>}
          <MarkdownContent markdown={previewMarkdown ?? value} fromPath={fromPath} />
        </TabsContent>
      </Tabs>
    </div>
  )
}
