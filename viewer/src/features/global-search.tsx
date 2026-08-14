import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { useNavigate } from "react-router-dom"
import { FileTextIcon } from "lucide-react"

import { apiGet, type ArticleSummary } from "@/lib/api"
import { Command, CommandDialog, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command"

type SearchResult = ArticleSummary & { snippet: string }

export function GlobalSearch({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const [query, setQuery] = useState("")
  const navigate = useNavigate()
  const results = useQuery({ queryKey: ["search", query], queryFn: () => apiGet<SearchResult[]>(`/api/search?q=${encodeURIComponent(query)}`), enabled: open && query.trim().length >= 2 })
  return <CommandDialog open={open} onOpenChange={onOpenChange} title="全文检索" description="搜索标题、别名、分类与正文" showCloseButton>
    <Command shouldFilter={false}><CommandInput value={query} onValueChange={setQuery} placeholder="输入至少两个字符" /><CommandList><CommandEmpty>{query.length < 2 ? "继续输入以搜索" : results.isFetching ? "正在搜索" : "没有结果"}</CommandEmpty><CommandGroup heading="本地知识库">{results.data?.map((item) => <CommandItem key={item.path} value={item.path} onSelect={() => { onOpenChange(false); navigate(`/${item.path}`) }}><FileTextIcon /><div className="min-w-0"><div className="font-medium">{item.title}</div><p className="truncate text-xs text-muted-foreground">{item.snippet || `${item.category_label} · ${item.aliases.join(" / ")}`}</p></div></CommandItem>)}</CommandGroup></CommandList></Command>
  </CommandDialog>
}
