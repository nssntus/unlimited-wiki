function isPrivateIpv4(hostname: string): boolean {
  const parts = hostname.split(".").map(Number)
  if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) return true
  const [a, b, c] = parts
  return a === 0 || a === 10 || a === 127 || a >= 224 ||
    (a === 100 && b >= 64 && b <= 127) || (a === 169 && b === 254) ||
    (a === 172 && b >= 16 && b <= 31) ||
    (a === 192 && (b === 0 || b === 168 || (b === 88 && c === 99))) ||
    (a === 198 && (b === 18 || b === 19 || b === 51)) ||
    (a === 203 && b === 0 && c === 113)
}

export function classifyPublicExternalUrl(raw: string): { href: string; insecure: boolean } | null {
  try {
    const parsed = new URL(raw)
    if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) return null
    const hostname = parsed.hostname.replace(/^\[|\]$/g, "").replace(/\.+$/, "").toLowerCase()
    if (!hostname || hostname === "localhost" || hostname.endsWith(".localhost") || hostname.endsWith(".local") || hostname.endsWith(".internal")) return null
    if (/^\d+(?:\.\d+){3}$/.test(hostname) && isPrivateIpv4(hostname)) return null
    // URL normalizes browser-compatible decimal/octal/hex IPv4 before this check.
    if (hostname.includes(":")) return null
    return { href: parsed.href, insecure: parsed.protocol === "http:" }
  } catch {
    return null
  }
}
