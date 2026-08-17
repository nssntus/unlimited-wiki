import { Link, Outlet } from "react-router-dom"
import { BookOpenIcon, FolderSearchIcon, LibraryIcon, LogInIcon, PanelsTopLeftIcon, SearchIcon, ShieldCheckIcon } from "lucide-react"

import { useSession } from "@/features/session-context"
import { Button } from "@/components/ui/button"

export function PublicShell() {
  const { session } = useSession()
  return <div className="min-h-svh bg-background text-foreground">
    <header className="sticky top-0 z-20 border-b bg-background/95 backdrop-blur">
      <div className="mx-auto flex h-14 max-w-6xl items-center justify-between gap-4 px-4">
        <Link to="/square" className="flex items-center gap-2 font-semibold"><BookOpenIcon className="size-5 text-primary" />Wiki 广场</Link>
        <nav className="flex min-w-0 items-center gap-1 sm:gap-2">
          <Button aria-label="搜索广场" size="icon-sm" variant="ghost" render={<Link to="/square/search" />}><SearchIcon /></Button>
          <Button aria-label="公共分类" size="icon-sm" variant="ghost" render={<Link to="/square/categories" />}><FolderSearchIcon /></Button>
          <Button aria-label="精选专题" size="icon-sm" variant="ghost" render={<Link to="/square/collections" />}><PanelsTopLeftIcon /></Button>
          {session?.authenticated ? <>
            <Button aria-label="我的广场互动" size="icon-sm" variant="ghost" render={<Link to="/square/library" />}><LibraryIcon /></Button>
            <Button className="hidden sm:inline-flex" variant="ghost" render={<Link to="/" />}>我的 Wiki</Button>
            {session.user?.role === "admin" && <Button aria-label="审核后台" size="icon-sm" variant="ghost" render={<Link to="/admin/reviews" />}><ShieldCheckIcon /></Button>}
          </> : <Button variant="outline" render={<Link to="/login" />}><LogInIcon data-icon="inline-start" />登录</Button>}
        </nav>
      </div>
    </header>
    <Outlet />
  </div>
}
