# AnyKir - remote desktop in one exe

Copy `dist\AnyKir.exe` to both PCs and run it:
- **Share this PC** - on the PC you want to control
- **Connect to a PC** - on the PC you sit at

No accounts, no time limits, end-to-end encrypted (AES-256-GCM, key derived from the password).

## Quick start
1. **PC to control:** *Share this PC* shows an **Address** (like `100.64.1.5:7001`) and a **Password**.
   **Change** sets your own password (8+ characters). Closing the window keeps it running in the tray.
2. **Your PC:** *Connect to a PC*, enter the address and password, click Connect.

Allow AnyKir through Windows Firewall when asked.

## Reaching the other PC
- **Tailscale (easiest):** install it on both PCs with the same account and use the `100.x.x.x` address.
- **Port forwarding:** forward TCP 7001 to the PC and use its public IP.
- **Relay:** run `AnyKir.exe --relay-server --key SECRET` on a reachable machine, then enter the relay and key
  on both sides (the host also shows an ID to enter).

## Admin windows, UAC prompts and the sign-in screen
*Share this PC* > **Advanced settings** > **Install admin service** (asks for administrator permission).
The service then shares the PC instead of the window:
- It starts with Windows and is the only thing listening on the port. The app leaves the port to it
  and shows "Shared through the admin service".
- It follows the app: password, port, relay and "Allow remote control" changes apply within seconds.
- It updates itself to the same signed version as the app within a minute.
- Screen and input only: file transfer and clipboard need the normal app (remove the service to use them).

**Reinstall** / **Remove** are in the same place. If another program holds the port, the window shows
**Free port**. Command line: `AnyKir.exe --install-service`, `--uninstall-service`, `--free-port 7001`.
Files live in `C:\Program Files\AnyKirService`, settings in `C:\ProgramData\AnyKirService` (admin only).

## Updates
Every AnyKir checks for updates every 5 minutes and installs a newer signed release when nobody is
connected. To publish (on the PC with the signing key):
```
python publish.py 1.2.0                     # serves it from this PC on port 7002 (via Tailscale)
python publish.py 1.2.0 --feed D:\folder    # also writes latest.json + exe to a folder or web server
```
Other locations can be set in *Advanced > Update source*. Only releases signed with
`%USERPROFILE%\.anykir-signing\private.pem` are accepted - **back that key up**.

## Limitations
- Without the admin service, Windows blocks input to admin windows, UAC prompts and the lock screen.
- No audio; text clipboard only. Use the **Keys** menu for Alt+Tab, Win and similar shortcuts.
- Connecting a PC to itself doesn't work.

## Developing
`pip install -r requirements.txt`, `python anykir.py`, `python -m pytest test_service.py`, `python test_e2e.py`.
`build.bat` builds `dist\AnyKir.exe` with the admin service packed inside.
