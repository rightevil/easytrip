import { useEffect, useState } from 'react';
import { api } from './api';
import type { AppSettings, ConfigurationChecks, SettingName, SettingsImport } from './api';

const groups: { title: string; description: string; fields: [SettingName, string, string][] }[] = [
  { title: '小红书', description: '填写已启动并完成登录的小红书 MCP 服务地址。EasyTrip 连接该服务，不代替它启动或登录。', fields: [
    ['xhs_mcp', '小红书 MCP 地址', 'http://127.0.0.1:18060/mcp'],
  ] },
  { title: '高德地图', description: '地点搜索与路线查询使用 Web 服务 Key；地图展示使用 JS Key 和安全密钥。', fields: [
    ['amap_web_key', '高德 Web 服务 Key', 'AMAP_WEB_KEY'],
    ['amap_js_key', '高德 JS Key', 'AMAP_JS_KEY'],
    ['amap_js_secure_key', '高德 JS 安全密钥', 'AMAP_JS_SECURE_KEY'],
    ['amap_mcp_url', '高德 MCP 地址（可选）', '留空时使用 Web 服务 Key 自动生成'],
  ] },
  { title: '分析模型', description: '填写兼容 Chat Completions 的服务地址与支持图文分析的模型。对应原 .env 中的 AGNES_URL / AGNES_KEY / AGNES_MODEL。', fields: [
    ['model_base_url', '模型服务地址', 'https://服务域名/v1'],
    ['model_api_key', '模型 API Key', 'MODEL_API_KEY / AGNES_KEY'],
    ['model_id', '模型名称', 'MODEL_ID / AGNES_MODEL'],
  ] },
  { title: '12306', description: '用于查询车站与列车信息；需要单独启动 12306 MCP 服务。', fields: [
    ['rail_mcp', '12306 MCP 地址', 'http://127.0.0.1:18061/mcp'],
  ] },
];

const fieldLabels = Object.fromEntries(groups.flatMap(group => group.fields.map(([name, label]) => [name, label])));
const serviceLabels: Record<string, string> = { xhs_mcp: '小红书攻略', model: '分析模型', amap_web: '地点搜索与路线', amap_js: '地图展示', rail_mcp: '12306 查询（可选）', amap_mcp: '高德 MCP 地址（可选）' };
const checkLabels = { invalid: '格式有误', unconfigured: '尚未配置', incomplete: '配置未齐', configured_not_probed: '配置齐全，未测试连接' };

function Checks({ checks }: { checks: ConfigurationChecks }) {
  return <ul className="settings-checks">{Object.entries(checks).map(([name, check]) => <li key={name}>
    <strong>{serviceLabels[name]}</strong><span>{name === 'amap_mcp' && check.state === 'unconfigured' ? '使用 Web 服务 Key 自动生成' : checkLabels[check.state]}</span>
    {!!check.missing.length && name !== 'amap_mcp' && <small>待填：{check.missing.map(field => fieldLabels[field]).join('、')}</small>}
    {!!check.invalid.length && <small className="inline-error">请修正：{check.invalid.map(field => fieldLabels[field]).join('、')}</small>}
  </li>)}</ul>;
}

export function Settings({ onClose, onboarding = false }: { onClose: () => void; onboarding?: boolean }) {
  const [saved, setSaved] = useState<AppSettings | null>(null);
  const [changes, setChanges] = useState<Partial<Record<SettingName, string>>>({});
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [show, setShow] = useState(false);
  const [checks, setChecks] = useState<ConfigurationChecks | null>(null);
  const [pendingImport, setPendingImport] = useState<{ content: string; preview: SettingsImport } | null>(null);
  const load = async () => {
    setBusy(true); setError(''); setNotice('');
    try { const result = await api.settings(); setSaved(result); setChecks(result.checks); setChanges({}); setPendingImport(null); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '无法读取设置'); }
    finally { setBusy(false); }
  };
  useEffect(() => { void load(); }, []);
  const save = async () => {
    if (!saved) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const result = await api.saveSettings(saved.revision, changes);
      setSaved(result); setChecks(result.checks); setChanges({}); setShow(false);
      setNotice(result.restart_required ? '设置已保存。请关闭并重新打开 EasyTrip 后使用新配置；网页版请重启后端服务。' : '设置已保存。');
    } catch (reason) { setError(reason instanceof Error ? reason.message : '设置未保存'); }
    finally { setBusy(false); }
  };
  const edit = (name: SettingName, value: string | undefined) => {
    setNotice(''); setChecks(null);
    setChanges(previous => {
      const next = { ...previous };
      if (value === undefined) delete next[name]; else next[name] = value;
      return next;
    });
  };
  const check = async () => {
    if (!saved) return;
    setBusy(true); setError(''); setNotice('');
    try { setChecks((await api.checkSettings(saved.revision, changes)).checks); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '配置检查未完成'); }
    finally { setBusy(false); }
  };
  const previewImport = async (file: File) => {
    if (!saved) return;
    setBusy(true); setError(''); setNotice(''); setPendingImport(null);
    try {
      if (file.size > 65536) throw new Error('请选择不超过 64 KB 的 UTF-8 .env 文件');
      const content = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.onerror = () => reject(new Error('无法读取所选文件'));
        reader.readAsText(file, 'utf-8');
      });
      const preview = await api.importSettings(saved.revision, content);
      setPendingImport({ content, preview });
    } catch (reason) { setError(reason instanceof Error ? reason.message : '无法读取配置文件'); }
    finally { setBusy(false); }
  };
  const applyImport = async () => {
    if (!saved || !pendingImport) return;
    setBusy(true); setError('');
    try {
      const result = await api.importSettings(saved.revision, pendingImport.content, true);
      if (!result.settings) throw new Error('未收到导入保存结果，请重新读取设置核对');
      setSaved(result.settings); setChecks(result.settings.checks); setPendingImport(null); setShow(false);
      setNotice(result.settings.restart_required ? '配置已导入并保存。关闭并重新打开 EasyTrip 后生效；网页版请重启后端。' : '配置已导入并保存。');
    } catch (reason) { setError(reason instanceof Error ? reason.message : '导入未完成'); }
    finally { setBusy(false); }
  };
  const close = async () => {
    if (!onboarding) { onClose(); return; }
    setBusy(true); setError('');
    try { await api.completeSetup(); onClose(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '无法保存引导状态'); }
    finally { setBusy(false); }
  };
  return <section className="settings-page" aria-labelledby="settings-title">
    <header className="workspace-header"><div><h1 id="settings-title">{onboarding ? '欢迎使用 EasyTrip' : '设置'}</h1><p className="muted">配置这台电脑使用的服务，所有行程共用。</p></div><button disabled={busy} onClick={() => { void close(); }}>{onboarding ? '稍后设置，先建行程' : '返回行程'}</button></header>
    {onboarding && <section className="panel settings-group" aria-label="首次使用引导"><h2>完成配置，再开始发现攻略</h2><ol className="settings-steps"><li>有旧版 .env 文件可直接导入，也可以在下方填写。</li><li>攻略发现需要小红书 MCP 和分析模型；地点搜索与地图需要高德配置。</li><li>保存配置后重启 EasyTrip。小红书 MCP 需单独启动并登录，12306 MCP 按需启用。</li></ol><p className="muted">现在也可以跳过，先创建和保存行程，再从左侧「设置」补齐。</p></section>}
    <p className="settings-intro">密钥和服务地址保存在本机配置文件中，已保存的内容不会回显。留空保留原值，点「清除」才会移除。保存后重启生效。</p>
    {error && <p className="inline-error" role="alert">{error}</p>}
    {notice && <p className="settings-notice" role="status">{notice}</p>}
    {!saved && <p role="status">{busy ? '正在读取设置…' : '设置尚未加载'}</p>}
    <section className="panel settings-group" aria-labelledby="import-title"><h2 id="import-title">导入已有 .env</h2><p className="muted">只在本机读取并预览，不发送给外部服务。非空配置会替换同名项；空值和无关项跳过。</p>
      <label className="settings-file">选择 .env 文件<input type="file" disabled={busy || !saved || !!Object.keys(changes).length || !!pendingImport} onChange={event => {
        const file = event.target.files?.[0]; event.target.value = ''; if (file) void previewImport(file);
      }} /></label>
      {!!Object.keys(changes).length && <p className="muted">请先保存当前编辑，或重新读取设置后再导入。</p>}
      {pendingImport && <div className="settings-import-preview"><p>将更新 {pendingImport.preview.imported_fields.length} 项：{pendingImport.preview.imported_fields.map(name => fieldLabels[name]).join('、')}。</p><p className="muted">跳过 {pendingImport.preview.skipped_empty} 个空值、{pendingImport.preview.ignored_fields} 个无关项；其他配置保留。</p>
        <Checks checks={pendingImport.preview.checks} /><div className="settings-actions"><button className="primary" disabled={busy} onClick={() => { void applyImport(); }}>确认导入并保存</button><button disabled={busy} onClick={() => setPendingImport(null)}>取消导入</button></div>
      </div>}
    </section>
    <section className="panel settings-group" aria-labelledby="checks-title"><h2 id="checks-title">本机配置检查</h2><p className="muted">检查必填项和格式，不连接外部服务、不调用模型。配置齐全不代表 Key 有效、服务已启动或小红书已登录。保存后重启才会应用修改。</p>
      {checks ? <Checks checks={checks} /> : <p>当前编辑尚未检查。</p>}
      <button disabled={busy || !saved || !!pendingImport} onClick={() => { void check(); }}>检查当前配置</button>
    </section>
    <form onSubmit={event => { event.preventDefault(); void save(); }}>
      <fieldset disabled={busy || !saved || !!pendingImport} className="settings-fields"><legend className="sr-only">服务配置</legend>
        <label className="settings-show"><input type="checkbox" checked={show} onChange={event => setShow(event.target.checked)} />显示本次输入的内容</label>
        {groups.map(group => <section className="panel settings-group" key={group.title}><h2>{group.title}</h2><p className="muted">{group.description}</p>
          {group.fields.map(([name, label, placeholder]) => <div className="settings-field" key={name}>
            <label htmlFor={`setting-${name}`}>{label}<span className="muted">{changes[name] === '' ? ' · 将清除' : saved?.fields[name].configured ? ' · 已配置' : ' · 未配置'}</span></label>
            <div className="settings-input"><input id={`setting-${name}`} type={name === 'model_id' || show ? 'text' : 'password'} autoComplete="off" spellCheck={false} maxLength={4096}
              value={changes[name] ?? saved?.fields[name].value ?? ''} placeholder={saved?.fields[name].configured ? '已配置，留空保留；输入新值替换' : placeholder}
              onChange={event => edit(name, event.target.value || undefined)} />
              <button type="button" onClick={() => edit(name, changes[name] === '' ? undefined : '')}>{changes[name] === '' ? '取消清除' : '清除'}<span className="sr-only">{label}</span></button>
            </div>
          </div>)}
        </section>)}
      </fieldset>
      <div className="settings-actions"><button className="primary" type="submit" disabled={busy || !saved || !Object.keys(changes).length}>{busy ? '处理中…' : '保存设置'}</button><button type="button" disabled={busy} onClick={() => { void load(); }}>重新读取设置</button>{saved?.restart_required && <span>有已保存的修改，重启后生效</span>}</div>
    </form>
    {saved && <p className="muted settings-path">本机配置：{saved.config_path}<br />此文件包含明文凭证，请勿分享。</p>}
  </section>;
}
