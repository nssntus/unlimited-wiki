import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { BookPlusIcon, ListTodoIcon } from "lucide-react"

import { apiGet, queryKeys, type TodoItem } from "@/lib/api"
import { GenerationDialog, type GenerationRequest } from "@/features/generation-dialog"
import { PageFrame, PageTitle } from "@/components/page-frame"
import { Button } from "@/components/ui/button"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Skeleton } from "@/components/ui/skeleton"

export function TodoPage() {
  const [generation, setGeneration] = useState<GenerationRequest | null>(null)
  const todo = useQuery({ queryKey: queryKeys.todo, queryFn: () => apiGet<{ items: TodoItem[]; scanning: boolean }>("/api/todo"), refetchInterval: (query) => query.state.data?.scanning ? 1000 : false })
  return <><PageFrame><div className="mx-auto max-w-4xl"><PageTitle eyebrow="候选治理" title="待写概念" description="只显示跨文档重复出现且尚无正本的概念；种子文档的全部候选词直接在正文中高亮。" />
    {todo.isLoading ? <Skeleton className="h-72 w-full" /> : todo.data?.items.length ? <div className="flex flex-col divide-y border-y">{todo.data.items.map((item) => <Collapsible key={item.term}><div className="flex items-center justify-between gap-4 py-4"><CollapsibleTrigger className="min-w-0 flex-1 text-left"><span className="font-medium">{item.term}</span><span className="ml-2 text-xs text-muted-foreground">{item.mentions} 篇提及</span></CollapsibleTrigger><Button size="sm" onClick={() => setGeneration({ keyword: item.term, from_path: item.sources[0]?.path || "", heading: item.sources[0]?.heading || "", passage: item.sources[0]?.passage || "" })}><BookPlusIcon data-icon="inline-start" />生成</Button></div><CollapsibleContent><div className="flex flex-col gap-4 pb-5 pl-4">{item.sources.map((source, index) => <div key={`${source.path}-${index}`} className="border-l-2 pl-4"><div className="text-xs font-medium">{source.title} · {source.path}</div><p className="mt-1 text-sm leading-6 text-muted-foreground">{source.passage}</p></div>)}</div></CollapsibleContent></Collapsible>)}</div> : <Empty className="min-h-72"><EmptyHeader><EmptyMedia variant="icon"><ListTodoIcon /></EmptyMedia><EmptyTitle>没有待写候选</EmptyTitle><EmptyDescription>请直接点击种子正文中的虚线关键词生成词条。</EmptyDescription></EmptyHeader></Empty>}
  </div></PageFrame><GenerationDialog request={generation} onOpenChange={(open) => !open && setGeneration(null)} /></>
}
