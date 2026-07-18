import * as vscode from 'vscode';
import { ChatPanel } from './chatPanel';

export function activate(context: vscode.ExtensionContext) {
  console.log('AWS Cheppu is now active');

  const openChat = vscode.commands.registerCommand('awscheppu.openChat', () => {
    ChatPanel.createOrShow(context.extensionUri, context);
  });

  context.subscriptions.push(openChat);

  // Auto-open on activation
  ChatPanel.createOrShow(context.extensionUri, context);
}

export function deactivate() {}
