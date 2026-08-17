import { Link, Outlet } from "react-router-dom"
import { ShieldCheckIcon } from "lucide-react"

import { Button } from "@/components/ui/button"

export function AdminShell() {
  return <div className="min-h-svh bg-background"><header className="border-b"><div className="mx-auto flex min-h-14 max-w-6xl flex-wrap items-center justify-between gap-2 px-4 py-2"><Link to="/admin/reviews" className="flex items-center gap-2 font-semibold"><ShieldCheckIcon className="size-5 text-primary" />审核后台</Link><nav className="flex flex-wrap items-center gap-1"><Button variant="ghost" render={<Link to="/admin/reviews" />}>投稿</Button><Button variant="ghost" render={<Link to="/admin/reports" />}>举报</Button><Button variant="ghost" render={<Link to="/admin/content" />}>内容</Button><Button variant="ghost" render={<Link to="/admin/square" />}>分类与策展</Button><Button variant="ghost" render={<Link to="/square" />}>广场</Button><Button variant="ghost" render={<Link to="/" />}>我的 Wiki</Button></nav></div></header><Outlet /></div>
}
