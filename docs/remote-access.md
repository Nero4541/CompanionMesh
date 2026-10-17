# Reaching the server from outside

A board carried outdoors (on your phone's hotspot or mobile data) is outside the server's network. The board only ever connects out to the server, so all it needs is one address of the server it can reach from anywhere; settings, logs, diagnostics and updates then travel over that same connection.

**Recommended: [Tailscale](https://tailscale.com) on the server and the board.** Both connect out to Tailscale, so it works behind carrier-grade NAT, on any hotspot or public Wi-Fi, with a VPN on the server and without touching the router or the firewall; the server's Tailscale address stays the same wherever the board is, and traffic between them is encrypted.

1. **Server:** install Tailscale and sign in. `tailscale ip -4` shows its address (`100.x.y.z`).
2. **Board:** install Tailscale and sign in to the same account (tailnet). On Sipeed's LicheeRV Nano image (no package manager), copy the static `riscv64` binaries from [pkgs.tailscale.com](https://pkgs.tailscale.com/stable/#static) to `/usr/sbin/` and start `tailscaled` at boot with its state on the SD card, since `/run` is cleared at every start, for example as `/etc/init.d/S45tailscale`:

   ```sh
   #!/bin/sh
   case "$1" in
     start)
       mkdir -p /root/.tailscale /var/run/tailscale
       start-stop-daemon -S -b -m -p /run/tailscaled.pid -x /usr/sbin/tailscaled -- \
         --state=/root/.tailscale/tailscaled.state --port=41641 ;;
     stop)
       start-stop-daemon -K -p /run/tailscaled.pid && rm -f /run/tailscaled.pid ;;
   esac
   ```

   Then run `tailscale up` once on the board and open the link it prints (or use `tailscale up --auth-key=…` with a key from the Tailscale admin console). `tailscaled` takes about 30 MB of the LicheeRV Nano's 128 MB.
3. **Set the board up** (or **Update over USB**) under **Boards** and pick the server's Tailscale address under **Server address for the board**: the list shows it, marked "Tailscale". The same address works at home and away.

Keep `server.host: 0.0.0.0` (or set it to the Tailscale address) and the device token, as for any network.

## When the server also runs a VPN

If the server's computer uses another VPN (Surfshark, NordVPN, ProtonVPN and the like), for privacy or to change its location often, Tailscale and that VPN have to share the machine. Without the steps below they get in each other's way: the VPN may not connect at all ("an issue with your connection"), or Tailscale reaches nothing while the VPN is on. On the server's computer:

1. **Tailscale: no exit node.** An exit node takes over all the computer's traffic, which is what the other VPN does too. In the Tailscale menu choose *Exit nodes, None*, or run `tailscale set --exit-node=`.
2. **Tailscale: do not use its DNS.** It otherwise answers every name lookup itself, and the VPN cannot reach its own servers. Run `tailscale set --accept-dns=false` (the menu: *Preferences*, turn off *Use Tailscale DNS settings*). Tailscale device names such as `my-server` then no longer resolve; use their `100.x` addresses, which is all the companion needs.
3. **The VPN: leave Tailscale out of it** (split tunneling; Surfshark calls it *Bypasser*, others *Split tunneling* or *Exclude apps*). Add the Tailscale **service**, `C:\Program Files\Tailscale\tailscaled.exe` on Windows (`tailscaled` on Linux), not the tray program `tailscale-ipn.exe`. Then reconnect the VPN. If the VPN has a kill switch that blocks excluded apps, turn it off.

The server itself (its Python) does not need to be excluded: its traffic to and from boards runs inside Tailscale, so the rest of what the server does (the agent, model downloads, updates) can keep going through the VPN.

Check it with the VPN connected:

```powershell
tailscale netcheck            # "UDP: true" and a nearest DERP region
tailscale ping 100.x.y.z      # the board: "via <address>" is direct, "via DERP(…)" is relayed; both work
```

Changing the VPN's location then does not touch the boards' connection, since Tailscale's own traffic does not go through the VPN. Tested on Windows with Surfshark (WireGuard): six location changes (Japan, Singapore, United States) with a LicheeRV Nano on a phone hotspot, without a single disconnection or a pause of more than three seconds in its live camera view.

## Without Tailscale: the public listener (advanced)

If you cannot use Tailscale and your home connection has a public IPv4 address (or IPv6), the server can offer boards a port of its own on the Internet. This needs a port forward on the router and breaks when the server's own traffic goes through a VPN or an exit node, so it is for those who can control both.

Turn on the **public listener** (`uv run companion setup` asks, or set it yourself):

```yaml
# configs/companion.local.yaml
server:
  host: 0.0.0.0
  public:
    enabled: true
    port: 8766
    url: wss://myhome.example.net:8766/v1/realtime   # how boards reach it from outside
```

It is a second listener, on IPv4 and IPv6, that serves only what boards need (`/health`, `/v1/realtime` and the edge client for updates), never the dashboard or the dev client. It takes only the tokens of boards this server set up, only in the `Authorization` header, refuses an address after repeated failed logins (`max_failed_auth`, `ban_s`) and limits connections per address. It uses TLS with this server's own certificate (made once in `data/tls/`); the installer copies that certificate to each board, which then trusts exactly it, so no domain or certificate authority is needed and the connection works with an IP address, a dynamic DNS name or IPv6. It is valid from 1970 on, as a board without network has no correct clock yet. Set the boards up again after turning it on, so they learn the address: they try their home address first and the public one when that fails.

Then make that port reachable from the Internet, depending on your connection:

| Your connection at home | What to do |
|---|---|
| A public IPv4 address | Forward TCP port 8766 on the router to the server's address. If the address changes, use dynamic DNS (most routers have it built in, or a free service) for `url`. |
| IPv6 (at home and on the phone's hotspot) | Allow incoming TCP port 8766 to the server's (stable) IPv6 address in the router's firewall, nothing else. No port forwarding is needed. |
| Carrier-grade NAT, no IPv6 | Use Tailscale (above). Otherwise a small rented server in between: a relay such as frp or an SSH reverse tunnel to it, or a hosted tunnel (Cloudflare Tunnel). |
| The server's traffic goes through a VPN or an exit node | Incoming connections usually fail (the replies leave through the VPN). Use Tailscale as described in [When the server also runs a VPN](#when-the-server-also-runs-a-vpn), or exclude the server from the VPN (split tunneling). |

And allow the port in the server's firewall, for that port only: on Windows, as administrator, `New-NetFirewallRule -DisplayName "Companion devices" -Direction Inbound -Protocol TCP -LocalPort 8766 -Action Allow -Profile Private`; on Linux, e.g. `sudo ufw allow 8766/tcp`. Never allow the whole program on all ports. Check from outside: on your phone, with Wi-Fi off, open `https://<your address>:8766/health`. The browser warns about the certificate (expected: it is the server's own); it should then say `status ok`.

**Security.** An open port is never free of risk, and the public listener is built to keep it small (see above). The largest risk is not the network but the agent: Hermes and OpenClaw can have terminal and file tools on their host, and anyone holding a board's token can talk to the agent. Lock out a board that is lost at once, and limit what the agent may do (its own tool and approval settings). Run the server as a normal user. A board's SD card is not encrypted: a stolen board leaks its token and its Wi-Fi keys, so also change the hotspot password.
