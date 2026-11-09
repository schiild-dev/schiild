#!/usr/bin/env python3
"""実行前フック: 外部への送信・本番の破壊的操作・能動スキャンを、実行の直前で止める。

OpenAI Codex の操作審査の基準（codex-rs/prompts/templates/guardian/policy.md）を、
宮﨑の絶対制約に合わせて機械判定に落としたもの。2026-11-09 制定。

  - 送信の許可は「送る中身」と「送り先」の両方が指定されて初めて成立する。
    作業の依頼は、その成果物を外へ出す許可を含まない。
  - 破壊的な操作は、対象と範囲が依頼で明示されていなければ確かめる。
  - 能動スキャンは営業目的では行わない（契約後の正式診断のみ）。

使い方（標準入力に PreToolUse の JSON が来る）:
  Claude Code: python3 agent_guard.py claude   → 該当すれば「確認」を返し、人が承認するまで実行しない
  Codex:       python3 agent_guard.py codex    → Codex は「確認」に未対応のため「拒否」を返す
                                                理由の文に、人に確認してから人が実行するよう書く
該当しなければ何も出力せず終了する（通常どおり実行される）。
判定できない入力は通す（このフックで作業を壊さない）。
"""
import json
import os
import re
import shlex
import subprocess
import sys

MODE = sys.argv[1] if len(sys.argv) > 1 else 'claude'

# 外部へ送る API（curl・wget などで直接叩く場合）
SEND_URLS = [
    (r'api\.line\.me/v2/bot/message/(push|multicast|broadcast|narrowcast)', 'LINE のメッセージ配信'),
    (r'api\.resend\.com/emails', 'メール送信（Resend）'),
    (r'api\.sendgrid\.com/v3/mail/send', 'メール送信（SendGrid）'),
    (r'api\.mailgun\.net/.*/messages', 'メール送信（Mailgun）'),
    (r'gmail\.googleapis\.com/.*/messages/send', 'メール送信（Gmail API）'),
    (r'slack\.com/api/chat\.(postMessage|scheduleMessage)|hooks\.slack\.com/services', 'Slack への投稿'),
    (r'discord(app)?\.com/api/webhooks', 'Discord への投稿'),
    (r'graph\.facebook\.com/.*/messages', 'WhatsApp などへの送信'),
    (r'graph\.threads\.net/.*/threads_publish', 'Threads への投稿'),
    (r'api\.(twitter|x)\.com/2/tweets', 'X への投稿'),
    (r'api\.twilio\.com/.*/Messages', 'SMS 送信'),
]
HTTP_CLIENTS = {'curl', 'wget', 'http', 'https', 'xh', 'httpie'}
MAIL_COMMANDS = {'sendmail', 'mail', 'mailx', 'mutt', 'msmtp', 'swaks'}
SCANNERS = {'nmap', 'masscan', 'zmap', 'nikto', 'sqlmap', 'hydra', 'medusa', 'gobuster', 'ffuf',
            'dirb', 'dirsearch', 'feroxbuster', 'nuclei', 'wpscan', 'zap-cli', 'zaproxy', 'arachni'}
WRAPPERS = {'sudo', 'env', 'command', 'exec', 'nohup', 'time', 'timeout', 'xargs', 'caffeinate', 'nice'}
SQL_WRITE = re.compile(r'\b(insert\s+into|update\s+\w+\s+set|delete\s+from|drop\s+(table|database|index|view)|alter\s+table|truncate|replace\s+into|create\s+table)\b', re.I)
SQL_DESTROY = re.compile(r'\b(drop\s+(table|database)|truncate(\s+table)?)\s+\w', re.I)
DELETE_NO_WHERE = re.compile(r'\bdelete\s+from\s+[\w."`]+\s*(;|$|["\'])', re.I)


def strip_heredocs(command):
    """ヒアドキュメントの本文（ファイルに書き込む文章など）は実行されないので判定から外す。"""
    out, lines, i = [], command.split('\n'), 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = re.search(r'<<-?\s*([\'"]?)([A-Za-z_][A-Za-z0-9_]*)\1', line)
        i += 1
        if m:
            end = m.group(2)
            while i < len(lines) and lines[i].strip() != end:
                i += 1
            i += 1
    return '\n'.join(out)


def segments(command):
    """コマンド列を ; && || | 改行で区切り、各部分の語の並びを返す。"""
    for part in re.split(r'\|\||&&|[;|\n]', strip_heredocs(command)):
        try:
            words = shlex.split(part, comments=True)
        except ValueError:
            words = part.split()
        while words and (re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', words[0]) or words[0] in WRAPPERS
                         or words[0].isdigit() or (words[0].startswith('-') and len(words) > 1)):
            words = words[1:]
        if words:
            yield words, part


def base(word):
    return word.rsplit('/', 1)[-1]


def current_branch(cwd):
    try:
        return subprocess.run(['git', '-C', cwd or '.', 'branch', '--show-current'],
                              capture_output=True, text=True, timeout=3).stdout.strip()
    except Exception:
        return ''


def check_bash(command, cwd):
    hits = []
    for words, raw in segments(command):
        cmd = base(words[0])
        args = words[1:]
        low = raw.lower()

        # 能動スキャン
        if cmd in SCANNERS:
            hits.append(('scan', f'能動スキャンの道具（{cmd}）。営業目的の調査では使わない。契約後の正式診断でだけ、本人の明示の指示で使う'))

        # 外部への送信
        if cmd in MAIL_COMMANDS:
            hits.append(('send', f'メール送信（{cmd}）'))
        if cmd in HTTP_CLIENTS or cmd in {'python', 'python3', 'node'}:
            for pat, label in SEND_URLS:
                if re.search(pat, raw):
                    hits.append(('send', label))
                    break
        if any(base(w) == 'wa_cli.py' for w in words) and '--send' in words:
            hits.append(('send', 'WhatsApp の送信（wa_cli.py --send）'))

        # git
        if cmd == 'git' and args[:1] == ['push']:
            rest = args[1:]
            if any(a in ('--force', '-f') or (a.startswith('-') and not a.startswith('--') and 'f' in a[1:])
                   for a in rest) or any(a.startswith('+') for a in rest):
                hits.append(('destroy', 'git の強制 push（--force-with-lease ではない）'))
            refs = [a for a in rest if not a.startswith('-')]
            if any(re.search(r'(^|:)(refs/heads/)?(main|master)$', r) for r in refs[1:]):
                hits.append(('prod', 'main への直接 push'))
            elif len(refs) <= 1 and '--all' not in rest and current_branch(cwd) in ('main', 'master'):
                hits.append(('prod', 'main ブランチ上での push（PR を通さない反映）'))
            if '--delete' in rest or any(r.startswith(':') for r in refs[1:]):
                hits.append(('destroy', 'リモートブランチの削除'))

        # 遠隔先への同期で --delete 付きの rsync
        if any(a.startswith('--delete') for a in args) and cmd == 'rsync':
            if any(re.match(r'^[\w.@-]+:', a) for a in args if not a.startswith('-')):
                hits.append(('destroy', '--delete 付きの rsync による遠隔先の削除を伴う同期（バックアップと一致確認の後に人が実行）'))

        # Cloudflare
        if cmd in ('wrangler', 'npx', 'pnpm', 'bunx') and 'wrangler' in words:
            w = words[words.index('wrangler'):] if 'wrangler' in words else words
            if len(w) > 2 and w[1] == 'd1' and w[2] == 'execute' and '--remote' in w:
                if '--file' in raw or SQL_WRITE.search(raw):
                    hits.append(('prod', '本番 D1 への書き込み（手元からの wrangler d1 execute --remote）'))
            if len(w) > 1 and w[1] in ('deploy', 'publish'):
                env = re.search(r'(--env|-e)[ =](\S+)', raw)
                if not env or env.group(2) not in ('dev', 'development', 'staging', 'stg', 'preview'):
                    hits.append(('prod', 'Workers の本番配備（wrangler deploy）'))
            if len(w) > 2 and w[1] in ('d1', 'kv', 'r2', 'queues') and w[2] in ('delete',):
                hits.append(('destroy', f'Cloudflare {w[1]} の削除'))

        # SQL の破壊的操作（どの DB クライアントでも）
        if cmd in ('mysql', 'sqlite3', 'psql', 'wp', 'wrangler', 'npx', 'pnpm') or 'd1' in words:
            if SQL_DESTROY.search(raw) or DELETE_NO_WHERE.search(raw):
                hits.append(('destroy', 'テーブルの削除・全件削除（DROP / TRUNCATE / WHERE なしの DELETE）'))

        # GitHub・Xserver の削除系
        if cmd == 'gh' and len(args) >= 2 and args[1] == 'delete' and args[0] in ('repo', 'release', 'secret', 'variable'):
            hits.append(('destroy', f'gh {args[0]} delete'))
        if cmd == 'xs' and args[:1] and args[0].upper() == 'DELETE':
            hits.append(('destroy', 'Xserver API の DELETE'))

        # ホームや根を丸ごと消す、HOME の上書き
        if cmd == 'rm' and any(a.startswith('-') and 'r' in a.lower() for a in args):
            targets = [a for a in args if not a.startswith('-')]
            if any(t in ('/', '~', '~/', '$HOME', '${HOME}', '$HOME/', '/Users', '/home', os.path.expanduser('~'), '.', '..', '*', '~/*', '$HOME/*')
                   for t in targets):
                hits.append(('wipe', f'rm -r の対象が広すぎる（{" ".join(targets)}）'))
    if re.search(r'(^|[\s;&|])(export\s+)?HOME=', command):
        hits.append(('destroy', 'HOME の上書き（以後の ~ や $HOME が別の場所を指す）'))
    return hits


def check_mcp(tool_name):
    name = tool_name.lower()
    if re.search(r'gmail.*__(send_message|forward|reply)$', name):
        return [('send', f'メール送信（{tool_name}）')]
    if re.search(r'__(send_message|post_message|chat_postmessage)$', name) and 'slack' in name:
        return [('send', f'Slack への投稿（{tool_name}）')]
    if re.search(r'__xserver_delete_', name):
        return [('destroy', f'Xserver の削除操作（{tool_name}）')]
    return []


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    tool = data.get('tool_name', '')
    tin = data.get('tool_input') or {}
    if tool == 'Bash' or tool in ('shell', 'local_shell', 'exec_command'):
        command = tin.get('command') if isinstance(tin, dict) else None
        if isinstance(command, list):
            command = ' '.join(command)
        hits = check_bash(command or '', data.get('cwd')) if command else []
    elif tool.startswith('mcp__'):
        hits = check_mcp(tool)
    else:
        hits = []
    if not hits:
        return

    kinds = {k for k, _ in hits}
    reasons = '、'.join(dict.fromkeys(r for _, r in hits))
    if MODE == 'codex':
        decision = 'deny'
        reason = (f'実行前フック（agent_guard）が止めました: {reasons}。'
                  'このコマンドを書き換えてすり抜けないこと。人に「何を・どこへ・なぜ」を示して確認し、'
                  '人が自分で実行する。依頼で明示されていたなら、その一文を引用して報告する。')
    else:
        decision = 'deny' if 'wipe' in kinds else 'ask'
        reason = (f'実行前フック（agent_guard）: {reasons}。'
                  '送る中身と送り先、または対象と範囲が依頼で明示されているかを確かめてから承認してください。')
    print(json.dumps({'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': decision,
        'permissionDecisionReason': reason,
    }}, ensure_ascii=False))


if __name__ == '__main__':
    main()
