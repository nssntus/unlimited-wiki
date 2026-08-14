import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { Link, Outlet, useLocation } from "react-router-dom"
import {
  ArchiveIcon,
  BellIcon,
  CheckCircle2Icon,
  FolderCogIcon,
  FilePenLineIcon,
  InboxIcon,
  ListTodoIcon,
  LogOutIcon,
  SearchIcon,
  SendIcon,
  Settings2Icon,
  ShieldCheckIcon,
  StoreIcon,
  WorkflowIcon,
  ListChecksIcon,
  RefreshCcwDotIcon,
} from "lucide-react"

import { apiGet, type ArticleSummary, type Category, type Notification, queryKeys } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { ScrollArea } from "@/components/ui/scroll-area"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarSeparator,
  SidebarTrigger,
} from "@/components/ui/sidebar"
import { Skeleton } from "@/components/ui/skeleton"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { GlobalSearch } from "@/features/global-search"
import { useSession } from "@/features/session-context"

const routes = [
  ["/inbox", "原料箱", InboxIcon],
  ["/classification", "待归类", ListChecksIcon],
  ["/categories", "分类管理", FolderCogIcon],
  ["/reconciliation", "文件对账", RefreshCcwDotIcon],
  ["/todo", "待写概念", ListTodoIcon],
  ["/health", "健康检查", CheckCircle2Icon],
  ["/tasks", "任务", WorkflowIcon],
  ["/submissions", "我的投稿", SendIcon],
  ["/notifications", "通知", BellIcon],
  ["/settings", "设置", Settings2Icon],
] as const

function articleHref(path: string) {
  return `/${path}`
}

export function WikiShell() {
  const location = useLocation()
  const [searchOpen, setSearchOpen] = useState(false)
  const { session, signOut } = useSession()
  const articles = useQuery({ queryKey: queryKeys.articles, queryFn: () => apiGet<ArticleSummary[]>("/api/articles") })
  const categories = useQuery({ queryKey: queryKeys.categories, queryFn: () => apiGet<Category[]>("/api/categories") })
  const notifications = useQuery({ queryKey: queryKeys.notifications, queryFn: () => apiGet<Notification[]>("/api/notifications"), refetchInterval: 15000 })
  const unreadNotifications = notifications.data?.filter((item) => !item.read_at).length ?? 0

  return (
    <SidebarProvider style={{ "--sidebar-width": "17rem" } as React.CSSProperties}>
      <Sidebar collapsible="offcanvas" className="border-r border-sidebar-border">
        <SidebarHeader className="gap-4 px-4 pt-4 pb-3">
          <Link to="/" className="flex items-center gap-2.5 font-semibold" aria-label="返回知识库">
            <span className="flex size-8 items-center justify-center rounded-md bg-primary text-primary-foreground"><ArchiveIcon /></span>
            <span className="text-base">知库</span>
          </Link>
          <div className="grid grid-cols-2 gap-2"><Button size="sm" variant="outline" render={<Link to="/square" />}><StoreIcon data-icon="inline-start" />广场</Button>{session?.user?.role === "admin" ? <Button size="sm" variant="outline" render={<Link to="/admin/reviews" />}><ShieldCheckIcon data-icon="inline-start" />审核</Button> : <span />}</div>
          <Button variant="outline" className="w-full justify-start text-muted-foreground" onClick={() => setSearchOpen(true)}>
            <SearchIcon data-icon="inline-start" />搜索标题、别名或正文
          </Button>
        </SidebarHeader>
        <SidebarSeparator />
        <SidebarContent>
          <ScrollArea className="h-full">
            <SidebarGroup>
              <SidebarGroupLabel>治理</SidebarGroupLabel>
              <SidebarGroupContent>
                <SidebarMenu>
                  {routes.map(([to, label, Icon]) => (
                    <SidebarMenuItem key={to}>
                      <SidebarMenuButton render={<Link to={to} />} isActive={location.pathname === to} tooltip={label}>
                        <Icon /><span>{label}</span>
                      </SidebarMenuButton>
                      {to === "/notifications" && unreadNotifications > 0 && <SidebarMenuBadge>{unreadNotifications}</SidebarMenuBadge>}
                    </SidebarMenuItem>
                  ))}
                </SidebarMenu>
              </SidebarGroupContent>
            </SidebarGroup>
            <SidebarSeparator />
            {articles.isLoading ? (
              <div className="flex flex-col gap-3 p-4"><Skeleton className="h-7 w-full" /><Skeleton className="h-7 w-5/6" /><Skeleton className="h-7 w-full" /></div>
            ) : categories.data?.map((category) => {
              const rows = (articles.data ?? []).filter((article) => article.primary_category_id === category.category_id)
              if (!rows.length) return null
              return (
                <SidebarGroup key={category.id}>
                  <SidebarGroupLabel>{category.label}</SidebarGroupLabel>
                  <SidebarGroupContent>
                    <SidebarMenu>
                      {rows.map((article) => (
                        <SidebarMenuItem key={article.path}>
                          <SidebarMenuButton
                            render={<Link to={articleHref(article.path)} />}
                            isActive={location.pathname === articleHref(article.path)}
                            tooltip={article.title}
                          >
                            <FilePenLineIcon /><span>{article.title}</span>
                          </SidebarMenuButton>
                          {article.content_status !== "词条" && <SidebarMenuBadge>{article.content_status}</SidebarMenuBadge>}
                        </SidebarMenuItem>
                      ))}
                    </SidebarMenu>
                  </SidebarGroupContent>
                </SidebarGroup>
              )
            })}
            {(articles.data ?? []).some((article) => article.classification_status !== "confirmed") && (
              <SidebarGroup>
                <SidebarGroupLabel>待归类</SidebarGroupLabel>
                <SidebarGroupContent><SidebarMenu>{(articles.data ?? []).filter((article) => article.classification_status !== "confirmed").map((article) => <SidebarMenuItem key={article.path}><SidebarMenuButton render={<Link to={articleHref(article.path)} />} isActive={location.pathname === articleHref(article.path)} tooltip={article.title}><FilePenLineIcon /><span>{article.title}</span></SidebarMenuButton><SidebarMenuBadge>待确认</SidebarMenuBadge></SidebarMenuItem>)}</SidebarMenu></SidebarGroupContent>
              </SidebarGroup>
            )}
          </ScrollArea>
        </SidebarContent>
        <SidebarFooter className="border-t px-4 py-3 text-xs text-muted-foreground">
          <div className="flex items-center justify-between gap-2"><div className="min-w-0"><div className="truncate font-medium text-foreground">{session?.user?.nickname}</div><div>{articles.data?.length ?? 0} 篇正本</div></div><Button size="icon-sm" variant="ghost" aria-label="退出登录" onClick={() => void signOut()}><LogOutIcon /></Button></div>
        </SidebarFooter>
      </Sidebar>
      <SidebarInset className="min-w-0 overflow-x-hidden">
        <header className="sticky top-0 z-20 flex h-14 shrink-0 items-center gap-3 border-b bg-background/95 px-4 backdrop-blur md:hidden">
          <Tooltip><TooltipTrigger render={<SidebarTrigger />} /><TooltipContent>打开导航</TooltipContent></Tooltip>
          <Link to="/" className="font-semibold">知库</Link>
        </header>
        <Outlet />
      </SidebarInset>
      <GlobalSearch open={searchOpen} onOpenChange={setSearchOpen} />
    </SidebarProvider>
  )
}
