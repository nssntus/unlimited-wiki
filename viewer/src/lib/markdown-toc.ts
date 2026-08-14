export function headingBase(text: string) {
  const normalized = text
    .normalize("NFKC")
    .trim()
    .toLocaleLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, "-")
    .replace(/^-+|-+$/g, "")
  return `section-${normalized || "heading"}`
}

export function nextHeadingId(text: string, counts: Map<string, number>) {
  const base = headingBase(text)
  const count = (counts.get(base) || 0) + 1
  counts.set(base, count)
  return count === 1 ? base : `${base}-${count}`
}

export function markdownToc(markdown: string) {
  const counts = new Map<string, number>()
  return [...markdown.matchAll(/^(#{2,6})\s+(.+)$/gm)].map((match) => ({
    depth: match[1].length,
    title: match[2].trim(),
    id: nextHeadingId(match[2], counts),
  }))
}

export function scrollToHeading(id: string) {
  const target = document.getElementById(id) || document.getElementById(headingBase(id))
  target?.scrollIntoView({ behavior: "smooth", block: "start" })
}
