/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * The page a site's setup link opens (https://<server>/site-setup#<id>.<secret>),
 * for the person at the site: no login. It shows what to download and run on
 * a machine there, and the setup code to paste into the agent.
 *
 * The secret is in the URL fragment, which the browser never sends to the
 * server: this page reads it, asks the server about the link by its id only
 * (that never uses the link up), and takes the fragment out of the address
 * bar and history. The agent itself checks the server before it sends the
 * secret (services/site_setup.py).
 */
import { useEffect, useMemo, useState } from 'react'
import { AlertCircle, Check, Copy, Download, Loader2, MonitorCog } from 'lucide-react'
import { siteService, type SetupInfo } from '../services/siteService'
import { AuthAlert, AuthLayout, authButton } from '../components/AuthLayout'

const STORE = 'opennvr.site-setup'

type Os = 'windows' | 'linux' | 'macos'

function readToken(): string | null {
  const frag = window.location.hash.replace(/^#/, '')
  if (/^[A-Za-z0-9_-]{4,16}\.[A-Za-z0-9]{16,64}$/.test(frag)) {
    sessionStorage.setItem(STORE, frag)
    // Out of the address bar and the history entry (a reload still works:
    // the tab keeps it in sessionStorage).
    window.history.replaceState(null, '', window.location.pathname)
    return frag
  }
  return sessionStorage.getItem(STORE)
}

function Copyable({ text, label }: { text: string; label: string }) {
  const [done, setDone] = useState(false)
  return (
    <div className="flex items-start gap-2">
      <code className="flex-1 break-all text-xs bg-[var(--bg)] border border-[var(--border)] px-2 py-1.5 font-mono">{text}</code>
      <button type="button" className="border border-[var(--border)] px-2 py-1.5" title={`Copy ${label}`}
        onClick={() => {
          void navigator.clipboard?.writeText(text).then(() => {
            setDone(true)
            window.setTimeout(() => setDone(false), 1500)
          })
        }}>
        {done ? <Check size={14} /> : <Copy size={14} />}
      </button>
    </div>
  )
}

export function SiteSetup() {
  const token = useMemo(readToken, [])
  const [info, setInfo] = useState<SetupInfo | null>(null)
  const [failed, setFailed] = useState(false)
  const [os, setOs] = useState<Os>(() =>
    /windows/i.test(navigator.userAgent) ? 'windows' : /mac os/i.test(navigator.userAgent) ? 'macos' : 'linux')

  useEffect(() => {
    // This page's address must not travel on as a Referer.
    const meta = document.createElement('meta')
    meta.name = 'referrer'
    meta.content = 'no-referrer'
    document.head.appendChild(meta)
    return () => { meta.remove() }
  }, [])
  useEffect(() => {
    if (!token) { setFailed(true); return }
    siteService.setupInfo(token.split('.')[0]).then(setInfo).catch(() => setFailed(true))
  }, [token])

  const host = window.location.host
  const code = token ? `${host}#${token}` : ''
  const dl = (platform: string) => `https://${host}/api/v1/site-agent/download/${platform}`

  if (failed) {
    return (
      <AuthLayout>
        <AuthAlert tone="error" icon={<AlertCircle size={16} />}>
          This setup link has expired, was already used, or is incomplete. Ask whoever sent it for a new one.
        </AuthAlert>
      </AuthLayout>
    )
  }
  if (!info) {
    return <AuthLayout><p className="flex items-center gap-2 text-sm"><Loader2 size={16} className="animate-spin" /> Checking the link…</p></AuthLayout>
  }
  return (
    <AuthLayout wide>
      <div className="space-y-4 text-sm">
        <div>
          <h1 className="flex items-center gap-2 text-lg font-semibold"><MonitorCog size={18} /> Connect "{info.site_name}" to OpenNVR</h1>
          <p className="text-[var(--text-dim)] mt-1">
            Do this on a computer at this site that is on the same network as the cameras and stays switched
            on: a small Linux box, a Raspberry Pi, or a Windows PC. It only connects out to {host}; nothing
            needs opening on the router. This link works once, until {new Date(info.expires_at).toLocaleString()}.
          </p>
        </div>

        <div className="flex gap-2" role="tablist" aria-label="The computer's system">
          {(['windows', 'linux', 'macos'] as Os[]).map((o) => (
            <button key={o} type="button" role="tab" aria-selected={os === o}
              className={`px-3 py-1.5 border border-[var(--border)] ${os === o ? 'bg-[var(--accent)] text-white' : ''}`}
              onClick={() => setOs(o)}>
              {o === 'windows' ? 'Windows' : o === 'linux' ? 'Linux / Raspberry Pi' : 'macOS'}
            </button>
          ))}
        </div>

        {os === 'windows' && (
          <ol className="list-decimal pl-5 space-y-3">
            <li>
              <a className={`${authButton} !w-auto px-4 inline-flex`} href={dl('windows-amd64')} download>
                <Download size={16} /> Download the OpenNVR agent
              </a>
            </li>
            <li>
              Open PowerShell in your Downloads folder and run:
              <Copyable text={'.\\opennvr-host.exe setup'} label="the command" />
              When it asks, paste the setup code (step 3).
            </li>
            <li>
              Setup code:
              <Copyable text={code} label="the setup code" />
            </li>
            <li>
              Then start it: <Copyable text={'.\\opennvr-host.exe site'} label="the command" />
            </li>
          </ol>
        )}
        {os === 'linux' && (
          <ol className="list-decimal pl-5 space-y-3">
            <li>
              Copy the setup code:
              <Copyable text={code} label="the setup code" />
            </li>
            <li>
              In a terminal, run this, and paste the code when it asks:
              <Copyable
                text={`curl -fsSLo opennvr-host "https://${host}/api/v1/site-agent/download/linux-$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/;s/armv7l/arm/')" && chmod +x opennvr-host && sudo ./opennvr-host setup`}
                label="the command" />
              It installs the agent as a service (it starts again after a reboot) and says when it is done.
            </li>
          </ol>
        )}
        {os === 'macos' && (
          <ol className="list-decimal pl-5 space-y-3">
            <li>
              In a terminal, download the agent:
              <Copyable
                text={`curl -fsSLo opennvr-host "https://${host}/api/v1/site-agent/download/darwin-$(uname -m | sed 's/x86_64/amd64/')" && chmod +x opennvr-host`}
                label="the download command" />
            </li>
            <li>
              Set it up, and paste the setup code (step 3) when it asks:
              <Copyable text="./opennvr-host setup" label="the command" />
            </li>
            <li>
              Setup code:
              <Copyable text={code} label="the setup code" />
            </li>
            <li>
              Then start it (keep this window open): <Copyable text="./opennvr-host site" label="the command" />
            </li>
          </ol>
        )}
        <p className="text-xs text-[var(--text-dim)]">
          Keep the setup code to yourself: it works once, and whoever uses it first connects this site. The agent
          checks that it is talking to {host} before it sends the code.
          {info.build ? ` Agent build ${info.build}.` : ''}
        </p>
      </div>
    </AuthLayout>
  )
}
