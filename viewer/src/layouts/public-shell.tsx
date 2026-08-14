import { Link, Outlet } from "react-router-dom"
import { BookOpenIcon, LogInIcon, ShieldCheckIcon } from "lucide-react"

import { useSession } from "@/features/session-context"
import { Button } from "@/components/ui/button"

export function PublicShell() {
  const { session } = useSession()
  return <div className="min-h-svh bg-background text-foreground">
    <header className="sticky top-0 z-20 border-b bg-background/95 backdrop-blur">
      <div className="mx-auto flex h-14 max-w-6xl items-center justify-between gap-4 px-4">
        <Link to="/square" className="flex items-center gap-2 font-semibold"><BookOpenIcon className="size-5 text-primary" />Wiki 广场</Link>
        <nav className="flex items-center gap-2">
          {session?.authenticated ? <>
            <Button variant="ghost" render={<Link to="/" />}>我的 Wiki</Button>
            {session.user?.role === "admin" && <Button variant="ghost" render={<Link to="/admin/reviews" />}><ShieldCheckIcon data-icon="inline-start" />审核后台</Button>}
          </> : <Button variant="outline" render={<Link to="/login" />}><LogInIcon data-icon="inline-start" />登录</Button>}
        </nav>
      </div>
    </header>
    <Outlet />
  </div>
}
