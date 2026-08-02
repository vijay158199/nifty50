# Deploying to Oracle Cloud "Always Free"

Genuinely free forever by Oracle's own policy for the resources below - no
monthly plan fee. More manual setup than Fly (a real Linux server you
configure yourself), but no bill.

This is a two-phase process:
- **Phase 1** happens in your browser (account signup, VM creation) -
  nobody but you can do this part (identity/card verification).
- **Phase 2** happens over SSH once the VM exists - tell Claude the VM's
  public IP and where the private key file is, and it's driven from there
  (installing Python, copying the project over, running it as a service).

## Phase 1: Account + VM (you do this)

### 1. Sign up
Go to **https://signup.oraclecloud.com**. You'll need:
- Email + phone verification
- A card (for identity verification only - Always Free resources are not
  billed as long as you stay within them; nothing here comes close to the
  limits)
- Pick your **Home Region** carefully - you cannot change it later, and
  Always Free resources are only available in your home region. Pick
  whichever region is closest to India with capacity (Oracle will suggest
  one - "India West (Mumbai)" or "India South (Hyderabad)" if offered are
  ideal for lowest latency to NSE/Yahoo Finance).

### 2. Create the VM instance
In the Oracle Cloud Console (after signup): **Menu → Compute → Instances → Create Instance**.

- **Name**: anything, e.g. `nifty50-dashboard`
- **Image and shape → Edit**:
  - Image: **Canonical Ubuntu 22.04** (or latest 22.04/24.04 LTS)
  - Shape: click "Change shape" → **Ampere** → **VM.Standard.A1.Flex** →
    set **1 OCPU / 6 GB memory** (comfortably inside the free 4 OCPU/24GB
    pool, plenty for this app). If Ampere capacity is unavailable in your
    region (common - high demand), fall back to shape **VM.Standard.E2.1.Micro**
    (always available, smaller: 1 OCPU/1GB - should still work, just leaner).
- **Networking**: leave defaults (creates a new VCN) - just make sure
  "Assign a public IPv4 address" is checked.
- **Add SSH keys**: choose **"Generate a key pair for me"**, then click
  **"Save private key"** - this downloads a `.key` file. **Save it
  somewhere you'll remember** (e.g. `C:\Users\VIJAY ROCK\Downloads\ssh-key.key`)
  - you need it to connect, and Oracle won't show it again.
- Click **Create**. Wait ~1-2 minutes for it to go "Running".

### 3. Open the firewall for port 8000
Two firewalls need opening (Oracle has both a cloud-level one and the
VM's own):
- **Console**: go to the instance's page → **Subnet** link → **Default
  Security List** → **Add Ingress Rule**: Source CIDR `0.0.0.0/0`,
  IP Protocol TCP, Destination Port Range `8000`.
- (The VM's own OS firewall will be opened over SSH in Phase 2.)

### 4. Note down two things for Phase 2
- The instance's **Public IP address** (shown on the instance's console page)
- The **path to the downloaded `.key` file**

## Phase 2: Server setup (Claude drives this over SSH)

Once you have the IP and key file, tell Claude both and it will:
1. SSH in and install Python 3.12, pip, venv
2. Copy this project over (via `scp`/`rsync`) to the VM
3. Set up the virtualenv and install `requirements.txt`
4. Open the VM's own OS firewall for port 8000
5. Set your real login credentials as environment variables (not the
   defaults - same reasoning as the Fly deploy: don't expose this with
   `vijay`/`changeme123`)
6. Create a `systemd` service so the app runs continuously and
   auto-restarts on crash or VM reboot (this is what makes it "always on"
   without you needing to keep an SSH session open)
7. Verify it's reachable and give you the final URL:
   `http://<your-vm-public-ip>:8000`

## Ongoing

- **Redeploying after code changes**: tell Claude to re-sync and restart
  the service - no need to redo Phase 1.
- **Checking it's running**: `ssh -i <key> ubuntu@<ip> "sudo systemctl status nifty50"`
- **Logs**: `ssh -i <key> ubuntu@<ip> "sudo journalctl -u nifty50 -f"`
- **Data persistence**: the SQLite DB, snapshots, and reports live directly
  on the VM's disk (`~/nifty50/data/`) - they survive reboots and restarts
  automatically, no extra volume setup needed (unlike Fly).
