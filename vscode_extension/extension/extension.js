const fs = require('fs');
const path = require('path');
const vscode = require('vscode');

let output;
let status;
let lastState = { alias: '', port: 0, mappingPath: '' };

function mappingPath() {
  const appData = process.env.APPDATA;
  return appData
    ? path.join(appData, 'SshTunnelManager', 'vscode-proxy-map.json')
    : '';
}

function sshAliasFromUri(uri) {
  if (!uri || uri.scheme !== 'vscode-remote' || !uri.authority) {
    return '';
  }
  const prefix = 'ssh-remote+';
  if (!uri.authority.toLowerCase().startsWith(prefix)) {
    return '';
  }
  try {
    return decodeURIComponent(uri.authority.slice(prefix.length));
  } catch (_error) {
    return uri.authority.slice(prefix.length);
  }
}

function currentRemoteAlias() {
  const folders = vscode.workspace.workspaceFolders || [];
  for (const folder of folders) {
    const alias = sshAliasFromUri(folder.uri);
    if (alias) {
      return alias;
    }
  }
  const editor = vscode.window.activeTextEditor;
  if (editor) {
    return sshAliasFromUri(editor.document.uri);
  }
  return '';
}

function readPort(alias) {
  const file = mappingPath();
  if (!file || !alias) {
    return { port: 0, file };
  }
  try {
    const payload = JSON.parse(fs.readFileSync(file, 'utf8'));
    const hosts = payload && payload.hosts && typeof payload.hosts === 'object'
      ? payload.hosts
      : {};
    const key = Object.keys(hosts).find(
      (item) => item.toLowerCase() === alias.toLowerCase(),
    );
    const port = key ? Number(hosts[key].port) : 0;
    return { port: Number.isInteger(port) && port > 0 && port <= 65535 ? port : 0, file };
  } catch (error) {
    output.appendLine(`[map] Unable to read ${file}: ${error.message}`);
    return { port: 0, file };
  }
}

function replaceEnvironment(collection, port) {
  const proxy = `http://127.0.0.1:${port}`;
  const values = {
    LC_STM_PROXY_PORT: String(port),
    http_proxy: proxy,
    https_proxy: proxy,
    HTTP_PROXY: proxy,
    HTTPS_PROXY: proxy,
    NO_PROXY: 'localhost,127.0.0.1,::1',
    no_proxy: 'localhost,127.0.0.1,::1',
  };
  collection.clear();
  collection.persistent = true;
  collection.description = `SSH Tunnel Manager proxy: ${proxy}`;
  for (const [name, value] of Object.entries(values)) {
    collection.replace(name, value);
  }
}

function clearEnvironment(collection) {
  collection.clear();
  collection.description = undefined;
}

function workspaceUris() {
  return (vscode.workspace.workspaceFolders || []).map((folder) => folder.uri.toString());
}

function writeDiagnostic(alias, port, state, detail = '') {
  const file = mappingPath();
  const payload = {
    updatedAt: new Date().toISOString(),
    processId: process.pid,
    remoteName: vscode.env.remoteName || '',
    alias,
    port,
    state,
    detail,
    workspaceUris: workspaceUris(),
  };
  const line = `[ssh-tunnel-manager-env] state=${state} remote=${payload.remoteName || '<local>'} alias=${alias || '<none>'} port=${port || '<none>'} folders=${JSON.stringify(payload.workspaceUris)}`;
  console.log(line);
  if (output) {
    output.appendLine(line);
  }
  if (!file) {
    return;
  }
  try {
    const folder = path.dirname(file);
    fs.mkdirSync(folder, { recursive: true });
    const target = path.join(folder, `vscode-extension-status-${process.pid}.json`);
    fs.writeFileSync(target, `${JSON.stringify(payload, null, 2)}\n`, 'utf8');
  } catch (error) {
    console.error(`[ssh-tunnel-manager-env] unable to write diagnostics: ${error.message}`);
  }
}

function applyEnvironment(context, announce = false) {
  const collection = context.environmentVariableCollection;
  if (vscode.env.remoteName !== 'ssh-remote') {
    clearEnvironment(collection);
    status.hide();
    lastState = { alias: '', port: 0, mappingPath: mappingPath() };
    writeDiagnostic('', 0, 'local-window');
    return;
  }

  const alias = currentRemoteAlias();
  const result = readPort(alias);
  if (!alias || !result.port) {
    clearEnvironment(collection);
    status.text = alias ? '$(warning) SSH proxy 未配置' : '$(warning) SSH proxy 等待文件夹';
    status.tooltip = alias
      ? `本机端口映射中没有 ${alias}`
      : '打开一个 Remote-SSH 文件夹后即可识别主机别名';
    status.show();
    lastState = { alias, port: 0, mappingPath: result.file };
    writeDiagnostic(alias, 0, 'missing-map-entry', status.tooltip);
    if (announce) {
      vscode.window.showWarningMessage(status.tooltip);
    }
    return;
  }

  replaceEnvironment(collection, result.port);
  status.text = `$(globe) SSH proxy ${result.port}`;
  status.tooltip = `${alias} 的新终端将使用 http://127.0.0.1:${result.port}`;
  status.show();
  lastState = { alias, port: result.port, mappingPath: result.file };
  output.appendLine(`[active] ${alias} -> http://127.0.0.1:${result.port}`);
  writeDiagnostic(alias, result.port, 'active');
  if (announce) {
    vscode.window.showInformationMessage(
      `${alias}：新终端将使用代理端口 ${result.port}；已打开的终端不会改变。`,
    );
  }
}

function activate(context) {
  output = vscode.window.createOutputChannel('SSH Tunnel Manager');
  status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 35);
  status.command = 'sshTunnelManager.showProxyEnvironment';
  context.subscriptions.push(output, status);

  const refresh = () => applyEnvironment(context, false);
  context.subscriptions.push(
    vscode.commands.registerCommand(
      'sshTunnelManager.refreshProxyEnvironment',
      () => applyEnvironment(context, true),
    ),
    vscode.commands.registerCommand(
      'sshTunnelManager.showProxyEnvironment',
      () => {
        const message = lastState.port
          ? `${lastState.alias}：新终端使用 http://127.0.0.1:${lastState.port}`
          : `尚未找到当前主机端口。映射文件：${lastState.mappingPath || '<不可用>'}`;
        vscode.window.showInformationMessage(message);
      },
    ),
    vscode.workspace.onDidChangeWorkspaceFolders(refresh),
    vscode.window.onDidChangeActiveTextEditor(refresh),
  );

  const file = mappingPath();
  if (file) {
    fs.watchFile(file, { interval: 1500 }, refresh);
    context.subscriptions.push({ dispose: () => fs.unwatchFile(file, refresh) });
  }
  applyEnvironment(context, false);
}

function deactivate() {}

module.exports = { activate, deactivate };
