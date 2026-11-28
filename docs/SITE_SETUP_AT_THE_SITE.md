# Connecting this site's cameras to OpenNVR

*For the person at the site. Five minutes, and nothing to change on the router.*

**You need:**
- the **setup link** you were sent;
- a computer **at this site, on the same network as the cameras**, that stays switched on: a Raspberry Pi, a small Linux box, or a Windows PC.

## 1. Open the link on that computer

It shows the name of this site and what to do next. If it says the link has expired or was used, ask for a new one.

## 2. Follow the steps for your computer

**Linux or Raspberry Pi**
1. Click **Copy** next to the setup code.
2. Open a terminal, paste the one line from the page, and press Enter.
3. When it asks, paste the setup code and press Enter.
4. Wait for **Done**. It says how many cameras it found.

That's all. It keeps working after a reboot.

**Windows**
1. Click **Download the OpenNVR agent**.
2. Open PowerShell in your Downloads folder and type `.\opennvr-host.exe setup`.
3. Paste the setup code when it asks.
4. Then type `.\opennvr-host.exe site`, and leave that window open.

**Mac**: the same as Windows, in Terminal: `./opennvr-host setup`, then `./opennvr-host site`.

## 3. Tell whoever sent the link

They add the cameras from their side. There's nothing else to do here.

## Good to know

- **Leave the computer on, and on wired network if you can.** If it's off, the cameras aren't recorded.
- **Keep the setup code to yourself.** It works once, and whoever uses it first connects this site.
- **Nothing needs opening on the router.** The computer only connects out to OpenNVR, like a web browser does.
- **If it says the server "could not prove it is the server"**, stop and tell whoever sent the link. Something on this network is intercepting secure connections, often a company firewall, and the agent will not send anything through it.
