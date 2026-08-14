import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { DownloadIcon, EyeIcon, EyeOffIcon, LogOutIcon, MoonIcon, RefreshCwIcon, SaveIcon, Settings2Icon, SunIcon, Trash2Icon, TriangleAlertIcon } from "lucide-react"
import { toast } from "sonner"

import { apiGet, apiPost, queryKeys, setCsrfToken, type ModelSettings } from "@/lib/api"
import { useTheme } from "@/components/theme-provider"
import { StatusBadge } from "@/components/markdown-content"
import { PageFrame, PageTitle } from "@/components/page-frame"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel, FieldLegend, FieldSet } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput } from "@/components/ui/input-group"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"

type Status = { configured: boolean; provider: string | null; model: string | null; web_fake_ip_allowed: boolean; network: string; queue: { active: number } }
type ModelCatalog = { models: string[] }
type ModelForm = { provider?: string; base_url?: string; api_key?: string; model?: string }

const providers = [
  { value: "openai", label: "OpenAI", baseUrl: "https://api.openai.com/v1" },
  { value: "deepseek", label: "DeepSeek", baseUrl: "https://api.deepseek.com/v1" },
  { value: "ollama", label: "Ollama", baseUrl: "http://127.0.0.1:11434/v1" },
  { value: "openai-compatible", label: "OpenAI-compatible", baseUrl: "" },
]

export function SettingsPage() {
  const client = useQueryClient()
  const status = useQuery({ queryKey: queryKeys.status, queryFn: () => apiGet<Status>("/api/status") })
  const settings = useQuery({ queryKey: queryKeys.modelSettings, queryFn: () => apiGet<ModelSettings>("/api/settings/model") })
  const [form, setForm] = useState<ModelForm>({})
  const [models, setModels] = useState<string[]>([])
  const [showKey, setShowKey] = useState(false)
  const [deletePassword, setDeletePassword] = useState("")
  const provider = form.provider ?? settings.data?.provider ?? ""
  const baseUrl = form.base_url ?? settings.data?.base_url ?? ""
  const apiKey = form.api_key ?? ""
  const model = form.model ?? settings.data?.model ?? ""
  const modelOptions = Array.from(new Set([model, ...models].filter(Boolean)))
  const { theme, setTheme } = useTheme()

  const loadModels = useMutation({
    mutationFn: () => apiPost<ModelCatalog>("/api/settings/models", { provider, base_url: baseUrl, api_key: apiKey }, false),
    onSuccess: (result) => {
      setModels(result.models)
      if (!model && result.models[0]) setForm((current) => ({ ...current, model: result.models[0] }))
      toast.success(result.models.length ? `已加载 ${result.models.length} 个模型` : "提供商未返回可选模型")
    },
  })
  const save = useMutation({
    mutationFn: () => apiPost<ModelSettings>("/api/settings/model", { provider, base_url: baseUrl, api_key: apiKey, model }),
    onSuccess: async (result) => {
      setForm({ provider: result.provider ?? "", base_url: result.base_url, model: result.model ?? "", api_key: "" })
      await Promise.all([
        client.invalidateQueries({ queryKey: queryKeys.modelSettings }),
        client.invalidateQueries({ queryKey: queryKeys.status }),
      ])
      toast.success("模型配置已保存并生效")
    },
    onError: (error) => toast.error(error.message),
  })
  const leaveAccount = () => { setCsrfToken(""); client.clear(); window.location.hash = "/login" }
  const revokeSessions = useMutation({ mutationFn: () => apiPost("/api/auth/sessions/revoke-all", {}), onSuccess: leaveAccount, onError: (error) => toast.error(error.message) })
  const deleteAccount = useMutation({ mutationFn: () => apiPost("/api/account/delete", { password: deletePassword }), onSuccess: leaveAccount, onError: (error) => toast.error(error.message) })

  const selectTheme = (values: string[]) => {
    const value = values[0]
    if (value === "light" || value === "dark" || value === "system") setTheme(value)
  }
  const selectProvider = (value: string | null) => {
    if (!value) return
    const suggestion = providers.find((item) => item.value === value)?.baseUrl ?? ""
    setForm((current) => ({ ...current, provider: value, base_url: baseUrl || suggestion, model: "" }))
    setModels([])
  }
  const canConnect = Boolean(provider && baseUrl)
  const canSave = Boolean(canConnect && model)

  return <PageFrame><div className="mx-auto max-w-3xl"><PageTitle eyebrow="本地运行" title="设置" description="模型凭据保存在本机，不会出现在状态接口、任务记录或日志中。" />
    {settings.isLoading || status.isLoading ? <Skeleton className="h-96 w-full" /> : <div className="flex flex-col gap-10">
      <FieldSet><FieldLegend>模型连接</FieldLegend><FieldGroup>
        <Field><FieldLabel>提供商</FieldLabel><Select value={provider} onValueChange={selectProvider}><SelectTrigger className="w-full"><SelectValue placeholder="选择模型提供商" /></SelectTrigger><SelectContent><SelectGroup>{providers.map((item) => <SelectItem key={item.value} value={item.value}>{item.label}</SelectItem>)}</SelectGroup></SelectContent></Select></Field>
        <Field><FieldLabel htmlFor="model-base-url">Base URL</FieldLabel><Input id="model-base-url" type="url" value={baseUrl} placeholder="http://127.0.0.1:8000/v1" onChange={(event) => { setForm((current) => ({ ...current, base_url: event.target.value, model: "" })); setModels([]) }} /><FieldDescription>支持 HTTP 和 HTTPS；URL 中不能包含用户名或密码。</FieldDescription></Field>
        <Field><FieldLabel htmlFor="model-api-key">API Key</FieldLabel><InputGroup><InputGroupInput id="model-api-key" type={showKey ? "text" : "password"} value={apiKey} placeholder={settings.data?.has_api_key ? "已保存，留空保持不变" : "本地模型可留空"} autoComplete="new-password" onChange={(event) => setForm((current) => ({ ...current, api_key: event.target.value }))} /><InputGroupAddon align="inline-end"><InputGroupButton aria-label={showKey ? "隐藏 API Key" : "显示 API Key"} onClick={() => setShowKey((current) => !current)}>{showKey ? <EyeOffIcon /> : <EyeIcon />}</InputGroupButton></InputGroupAddon></InputGroup><FieldDescription>保存后不会再次回显 Key。</FieldDescription></Field>
        <Field><FieldLabel>模型</FieldLabel>{models.length ? <Select value={model} onValueChange={(value) => value && setForm((current) => ({ ...current, model: value }))}><SelectTrigger className="w-full"><SelectValue placeholder="选择模型" /></SelectTrigger><SelectContent><SelectGroup>{modelOptions.map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}</SelectGroup></SelectContent></Select> : <Input value={model} placeholder="先加载模型，或输入模型 ID" onChange={(event) => setForm((current) => ({ ...current, model: event.target.value }))} />}<div className="mt-2 flex justify-start"><Button type="button" size="sm" variant="outline" disabled={!canConnect || loadModels.isPending} onClick={() => loadModels.mutate()}>{loadModels.isPending ? <Spinner data-icon="inline-start" /> : <RefreshCwIcon data-icon="inline-start" />}加载模型</Button></div>{loadModels.isError && <FieldError>{loadModels.error.message}</FieldError>}</Field>
      </FieldGroup><div className="mt-6 flex justify-end"><Button disabled={!canSave || save.isPending} onClick={() => save.mutate()}>{save.isPending ? <Spinner data-icon="inline-start" /> : <SaveIcon data-icon="inline-start" />}保存模型配置</Button></div></FieldSet>

      {baseUrl.toLowerCase().startsWith("http://") && <Alert variant="destructive"><TriangleAlertIcon /><AlertTitle>模型连接未加密</AlertTitle><AlertDescription>模型请求和 API Key 会通过 HTTP 明文传输。仅连接你信任的服务与网络。</AlertDescription></Alert>}

      <FieldSet><FieldLegend>运行状态</FieldLegend><FieldGroup>
        <Field orientation="responsive"><FieldLabel>模型</FieldLabel><div className="flex items-center gap-2"><StatusBadge value={status.data?.configured ? "已配置" : "未配置"} kind={status.data?.configured ? "good" : "warn"} /><span className="text-sm text-muted-foreground">{status.data?.model || "本地摘录模式"}</span></div></Field>
        <Field orientation="responsive"><FieldLabel>活动任务</FieldLabel><span className="text-sm">{status.data?.queue.active ?? 0}</span></Field>
        <Field orientation="responsive"><FieldLabel>主题</FieldLabel><ToggleGroup value={[theme]} onValueChange={selectTheme}><ToggleGroupItem value="light"><SunIcon data-icon="inline-start" />浅色</ToggleGroupItem><ToggleGroupItem value="dark"><MoonIcon data-icon="inline-start" />深色</ToggleGroupItem><ToggleGroupItem value="system"><Settings2Icon data-icon="inline-start" />系统</ToggleGroupItem></ToggleGroup><FieldDescription>主题偏好只保存在本机浏览器。</FieldDescription></Field>
      </FieldGroup></FieldSet>

      <FieldSet><FieldLegend>账号与数据</FieldLegend><FieldGroup>
        <Field orientation="responsive"><div><FieldLabel>导出私有空间</FieldLabel><FieldDescription>ZIP 只包含 Wiki 正本与 Raw 原料，不包含会话或模型密钥。</FieldDescription></div><Button variant="outline" render={<a href="/api/account/export" download />}><DownloadIcon data-icon="inline-start" />下载导出</Button></Field>
        <Field orientation="responsive"><div><FieldLabel>撤销全部会话</FieldLabel><FieldDescription>包括当前设备在内的所有登录会立即失效。</FieldDescription></div><Button variant="outline" disabled={revokeSessions.isPending} onClick={() => revokeSessions.mutate()}><LogOutIcon data-icon="inline-start" />全部退出</Button></Field>
        <Field><FieldLabel htmlFor="delete-account-password">删除账号</FieldLabel><FieldDescription>将撤回公开内容并删除私有空间和模型配置。请输入当前密码确认。</FieldDescription><Input id="delete-account-password" type="password" autoComplete="current-password" value={deletePassword} onChange={(event) => setDeletePassword(event.target.value)} /><div className="mt-2 flex justify-end"><Button variant="destructive" disabled={!deletePassword || deleteAccount.isPending} onClick={() => deleteAccount.mutate()}><Trash2Icon data-icon="inline-start" />永久删除账号</Button></div></Field>
      </FieldGroup></FieldSet>

      {status.data?.web_fake_ip_allowed && <Alert><TriangleAlertIcon /><AlertTitle>已兼容代理 Fake-IP</AlertTitle><AlertDescription>网页域名可使用本机透明代理的 Fake-IP 映射。</AlertDescription></Alert>}
    </div>}
  </div></PageFrame>
}
