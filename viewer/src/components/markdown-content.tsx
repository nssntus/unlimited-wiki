import { Children, cloneElement, isValidElement, type ComponentProps, type ReactNode } from "react"
import { Link } from "react-router-dom"
import ReactMarkdown from "react-markdown"
import rehypeSanitize, { defaultSchema } from "rehype-sanitize"
import remarkGfm from "remark-gfm"

import { cn } from "@/lib/utils"
import { nextHeadingId, scrollToHeading } from "@/lib/markdown-toc"

const schema = {
  ...defaultSchema,
  tagNames: [
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "a", "blockquote", "ul", "ol", "li", "pre", "code",
    "strong", "em", "del", "table", "thead", "tbody", "tr", "th", "td", "hr",
  ],
  attributes: {
    a: ["href", "title"],
    code: [["className", /^language-[\w-]+$/]],
  },
  protocols: { href: ["http", "https", "mailto"] },
}

function plainText(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node)
  if (Array.isArray(node)) return node.map(plainText).join("")
  if (isValidElement<{ children?: ReactNode }>(node)) return plainText(node.props.children)
  return ""
}

function resolveInternal(fromPath: string, href: string): string | null {
  if (!href || href.startsWith("#")) return null
  if (/^(https?:|mailto:)/i.test(href)) return null
  const clean = href.split("#", 1)[0]
  const start = fromPath.startsWith("raw/") ? fromPath.split("/").slice(0, -1) : fromPath.split("/").slice(0, -1)
  for (const part of clean.split("/")) {
    if (!part || part === ".") continue
    if (part === "..") start.pop()
    else start.push(part)
  }
  const target = start.join("/")
  return target.startsWith("raw/") ? `/raw/${target.replace(/^raw\//, "")}` : `/${target.replace(/^wiki\//, "")}`
}

type KeywordEntry = { term: string; path: string | null; title: string; kind: "page" | "missing" }

function enhanceText(node: ReactNode, keywords: KeywordEntry[], onMissing?: (term: string) => void): ReactNode {
  if (typeof node === "string") {
    const matches = keywords
      .filter((item) => item.term.length >= 2)
      .sort((a, b) => b.term.length - a.term.length)
    if (!matches.length) return node
    const pattern = new RegExp(`(${matches.map((item) => item.term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "gi")
    return node.split(pattern).map((part, index) => {
      const match = matches.find((item) => item.term.toLocaleLowerCase() === part.toLocaleLowerCase())
      if (!match) return part
      if (match.path) return <Link key={`${part}-${index}`} to={`/${match.path}`} className="font-medium text-link no-underline hover:underline">{part}</Link>
      return <button key={`${part}-${index}`} type="button" className="inline border-b border-dashed border-warning text-warning-foreground" onClick={() => onMissing?.(match.term)}>{part}</button>
    })
  }
  if (Array.isArray(node)) return node.map((child) => enhanceText(child, keywords, onMissing))
  if (isValidElement<{ children?: ReactNode; href?: string; to?: string }>(node)) {
    if (node.props.href || node.props.to) return node
    return cloneElement(node, { children: Children.map(node.props.children, (child) => enhanceText(child, keywords, onMissing)) })
  }
  return node
}

export function MarkdownContent({ markdown, fromPath, className, keywords = [], onMissingKeyword, publicMode = false }: { markdown: string; fromPath: string; className?: string; keywords?: KeywordEntry[]; onMissingKeyword?: (term: string) => void; publicMode?: boolean }) {
  const headingCounts = new Map<string, number>()
  const headingId = (children: ReactNode) => nextHeadingId(plainText(children), headingCounts)
  return (
    <div className={cn("wiki-prose min-w-0", className)}>
      <ReactMarkdown
        skipHtml
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[[rehypeSanitize, schema]]}
        urlTransform={(url) => {
          const compact = Array.from(url).filter((char) => char.charCodeAt(0) > 32).join("")
          return /^(javascript|data|vbscript|file|blob):/i.test(compact) ? "" : url
        }}
        components={{
          a({ href = "", children, ...props }) {
            const internal = resolveInternal(fromPath, href)
            if (internal) return publicMode ? <span>{children}</span> : <Link to={internal}>{children}</Link>
            if (/^https?:/i.test(href)) return <a href={href} target={publicMode ? "_blank" : undefined} rel="noopener noreferrer" {...props}>{children}{publicMode && <span className="sr-only">（外部来源，平台未核验）</span>}</a>
            if (href.startsWith("#")) {
              const id = decodeURIComponent(href.slice(1))
              return <button type="button" className="inline text-link underline underline-offset-4" onClick={() => scrollToHeading(id)}>{children}</button>
            }
            return <span {...props}>{children}</span>
          },
          h1: ({ children }) => <h1 className="sr-only">{children}</h1>,
          h2: ({ children }) => <h2 id={headingId(children)} className="scroll-mt-6">{enhanceText(children, keywords, onMissingKeyword)}</h2>,
          h3: ({ children }) => <h3 id={headingId(children)} className="scroll-mt-6">{enhanceText(children, keywords, onMissingKeyword)}</h3>,
          h4: ({ children }) => <h4 id={headingId(children)} className="scroll-mt-6">{enhanceText(children, keywords, onMissingKeyword)}</h4>,
          h5: ({ children }) => <h5 id={headingId(children)} className="scroll-mt-6">{enhanceText(children, keywords, onMissingKeyword)}</h5>,
          h6: ({ children }) => <h6 id={headingId(children)} className="scroll-mt-6">{enhanceText(children, keywords, onMissingKeyword)}</h6>,
          p: ({ children }) => <p>{enhanceText(children, keywords, onMissingKeyword)}</p>,
          li: ({ children }) => <li>{enhanceText(children, keywords, onMissingKeyword)}</li>,
          td: ({ children }) => <td>{enhanceText(children, keywords, onMissingKeyword)}</td>,
        }}
      >
        {markdown}
      </ReactMarkdown>
    </div>
  )
}

export function StatusBadge({ value, kind = "neutral" }: { value: string; kind?: "neutral" | "good" | "warn" | "bad" }) {
  const classes = {
    neutral: "border-border bg-muted text-muted-foreground",
    good: "border-success/20 bg-success-muted text-success-foreground",
    warn: "border-warning/20 bg-warning-muted text-warning-foreground",
    bad: "border-destructive/20 bg-destructive/10 text-destructive",
  }
  return <span className={cn("inline-flex h-5 items-center rounded-full border px-2 text-xs font-medium whitespace-nowrap", classes[kind])}>{value}</span>
}

export function InlinePath({ children, ...props }: ComponentProps<"code"> & { children: ReactNode }) {
  return <code className="break-all rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground" {...props}>{children}</code>
}
