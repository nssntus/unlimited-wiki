import { useMemo, useState } from "react"
import { CheckIcon, ChevronsUpDownIcon, PlusIcon, XIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { cn } from "@/lib/utils"

export type TaxonomySelection =
  | { kind: "existing"; id: string; name: string }
  | { kind: "create" | "proposal"; name: string }
  | { kind: "inbox"; name: string }

type Option = { id: string; name: string }

function key(value: string) {
  return value.normalize("NFKC").trim().toLocaleLowerCase()
}

export function CategoryPicker({
  options,
  value,
  onChange,
  createKind = "create",
  allowInbox = false,
}: {
  options: Option[]
  value: TaxonomySelection | null
  onChange: (value: TaxonomySelection) => void
  createKind?: "create" | "proposal"
  allowInbox?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState("")
  const exact = options.some((option) => key(option.name) === key(query))
  const clean = query.normalize("NFKC").trim()
  return <Popover open={open} onOpenChange={setOpen}>
    <PopoverTrigger render={<Button type="button" variant="outline" role="combobox" aria-expanded={open} className="w-full justify-between font-normal" />}>
      <span className={cn("truncate", !value && "text-muted-foreground")}>{value?.name || "搜索或选择分类"}</span><ChevronsUpDownIcon className="opacity-50" />
    </PopoverTrigger>
    <PopoverContent align="start" className="w-[min(24rem,calc(100vw-2rem))] p-0">
      <Command shouldFilter>
        <CommandInput value={query} onValueChange={setQuery} placeholder="搜索分类" />
        <CommandList>
          <CommandEmpty>没有匹配分类</CommandEmpty>
          <CommandGroup>
            {allowInbox && <CommandItem value="_inbox" onSelect={() => { onChange({ kind: "inbox", name: "暂不分类" }); setOpen(false) }}><span>暂不分类（_inbox）</span>{value?.kind === "inbox" && <CheckIcon className="ml-auto" />}</CommandItem>}
            {options.map((option) => <CommandItem key={option.id} value={option.name} onSelect={() => { onChange({ kind: "existing", ...option }); setOpen(false) }}><span className="min-w-0 flex-1 break-words">{option.name}</span>{value?.kind === "existing" && value.id === option.id && <CheckIcon className="ml-auto" />}</CommandItem>)}
            {clean && !exact && <CommandItem value={`create ${clean}`} onSelect={() => { onChange({ kind: createKind, name: clean }); setOpen(false); setQuery("") }}><PlusIcon /><span className="min-w-0 break-words">{createKind === "proposal" ? "提案" : "创建"}“{clean}”</span></CommandItem>}
          </CommandGroup>
        </CommandList>
      </Command>
    </PopoverContent>
  </Popover>
}

export function TagPicker({
  options,
  value,
  onChange,
  createKind = "create",
  maximum = 20,
}: {
  options: Option[]
  value: TaxonomySelection[]
  onChange: (value: TaxonomySelection[]) => void
  createKind?: "create" | "proposal"
  maximum?: number
}) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState("")
  const selectedKeys = useMemo(() => new Set(value.map((item) => item.kind === "existing" ? item.id : key(item.name))), [value])
  const clean = query.normalize("NFKC").trim()
  const exact = options.some((option) => key(option.name) === key(clean)) || value.some((item) => key(item.name) === key(clean))
  const toggle = (item: TaxonomySelection, identity: string) => {
    onChange(selectedKeys.has(identity) ? value.filter((current) => (current.kind === "existing" ? current.id : key(current.name)) !== identity) : value.length < maximum ? [...value, item] : value)
  }
  return <div className="flex flex-col gap-2">
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger render={<Button type="button" variant="outline" role="combobox" aria-expanded={open} className="w-full justify-between font-normal" />}>
        <span className="truncate text-muted-foreground">搜索或添加标签</span><ChevronsUpDownIcon className="opacity-50" />
      </PopoverTrigger>
      <PopoverContent align="start" className="w-[min(24rem,calc(100vw-2rem))] p-0">
        <Command shouldFilter>
          <CommandInput value={query} onValueChange={setQuery} placeholder="搜索标签" />
          <CommandList><CommandEmpty>没有匹配标签</CommandEmpty><CommandGroup>
            {options.map((option) => <CommandItem key={option.id} value={option.name} data-checked={selectedKeys.has(option.id)} onSelect={() => toggle({ kind: "existing", ...option }, option.id)}>{option.name}</CommandItem>)}
            {clean && !exact && <CommandItem value={`create ${clean}`} onSelect={() => { toggle({ kind: createKind, name: clean }, key(clean)); setQuery("") }}><PlusIcon />{createKind === "proposal" ? "提案" : "创建"}“{clean}”</CommandItem>}
          </CommandGroup></CommandList>
        </Command>
      </PopoverContent>
    </Popover>
    {!!value.length && <div className="flex flex-wrap gap-2">{value.map((item) => { const identity = item.kind === "existing" ? item.id : key(item.name); return <Button key={identity} type="button" size="sm" variant="secondary" onClick={() => toggle(item, identity)}>{item.name}{item.kind !== "existing" && <span className="text-xs text-muted-foreground">{item.kind === "proposal" ? "提案" : "新建"}</span>}<XIcon /></Button> })}</div>}
    <p className="text-xs text-muted-foreground">{value.length}/{maximum}</p>
  </div>
}
