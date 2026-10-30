# Deploy Hermes Federation on Oracle Cloud Always Free

Result: Hermes + 118 agents + the console run 24/7 on an Oracle server, reachable over HTTPS through
Cloudflare Tunnel, behind a username and password. No port is opened on the server.

## 1. Create the server (Oracle console)
1. Sign up at https://www.oracle.com/cloud/free/ (a card is asked for identity; Always Free resources are not charged — check the current terms).
2. Compute → Instances → **Create instance**
   - Image: **Ubuntu 22.04 or 24.04**
   - Shape: **Ampere A1 (VM.Standard.A1.Flex)**, 4 OCPU, 24 GB RAM (Always Free limit)
   - "Out of capacity"? Retry later or pick another availability domain.
   - Add your SSH public key, create.
3. Note the public IP. Connect: `ssh ubuntu@<IP>`

## 2. Prepare files on the PC
```powershell
# Hermes data (contains keys and auth: copy it privately, never via OneDrive or email)
hermes backup -o "$env:USERPROFILE\hermes-backups\hermes-federation.zip" -k 0
# Board workspaces
Compress-Archive "$env:USERPROFILE\hermes-federation\workspaces" "$env:USERPROFILE\hermes-backups\workspaces.zip"
```
Make a folder `hermes-deploy` containing:
- `hermes-federation.zip`, `workspaces.zip`
- `console\` (server.py, index.html)
- `roster\set_local_model.py`
- `setup_server.sh`, `hermes-console.service` (from this folder)

## 3. Copy to the server
```powershell
scp -r hermes-deploy ubuntu@<IP>:~/
```

## 4. Run the setup (on the server)
```bash
cd ~/hermes-deploy && bash setup_server.sh
```
It installs Hermes, restores the federation, fixes Windows paths, switches all agents to Groq
`qwen/qwen3.8-27b`, installs the gateway service and the console service.

## 5. Set the console login  ← where you change the password
```bash
sudo nano /etc/hermes-console.env
```
```
# no login settings here: Owner sign-in: on first start the console prints a one-time setup code; open /login, enter it and choose the owner password. Only a salted hash is stored (HERMES_HOME/federation/owner.json).
```
```bash
sudo chmod 600 /etc/hermes-console.env
sudo systemctl enable --now hermes-console      # first time
sudo systemctl restart hermes-console           # after every password change
```

## 6. Set the Groq key (use a NEW key, the old one was exposed)
Hermes reads keys per agent from each profile's `.env`. Run once (type your key in place of NEWKEY):
```bash
read -rs -p "Groq key: " K; echo
for f in ~/.hermes/.env ~/.hermes/profiles/*/.env; do
  sed -i '/^GROQ_API_KEY=/d' "$f"; echo "GROQ_API_KEY=$K" >> "$f"; chmod 600 "$f"
done; unset K
hermes gateway restart || hermes gateway start
```

## 7. HTTPS link with Cloudflare Tunnel
```bash
curl -fsSLo cf.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb
sudo dpkg -i cf.deb
cloudflared tunnel --url http://localhost:8765     # quick test: prints https://xxxx.trycloudflare.com
```
For a fixed link on your own domain: free Cloudflare account → Zero Trust → Tunnels → create a tunnel,
point it to `http://localhost:8765`, and run the `cloudflared service install <token>` command it gives you.
Optional extra lock: Cloudflare Access (email login) in front of the tunnel.

## 8. Check it works
- Open the link → browser asks for user/password → wrong password is refused.
- 118 agents listed, boards load.
- Chat with `isc-receptionist` answers.
- `systemctl status hermes-console` and `hermes gateway status` both running.

## Never
- Open Oracle security-list ports for 8765, 11434, databases or Hermes admin.
- Put the password or keys in Git, in these scripts, or in chat.
