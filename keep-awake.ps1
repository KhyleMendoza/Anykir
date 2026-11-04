#Requires -RunAsAdministrator
# WARNING: keeps this PC awake and unlocked; anyone with physical access can use it. Undo with -Revert.
[CmdletBinding()]
param([switch]$Revert)
$ErrorActionPreference = 'Stop'
$desktop = 'HKCU:\Control Panel\Desktop'
$policy  = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'
$lockGuid = '0e796bdb-100d-47d6-a2d5-f7d2daa51f51'

if ($Revert) {
    powercfg /change standby-timeout-ac 30; powercfg /change standby-timeout-dc 15
    powercfg /change monitor-timeout-ac 10; powercfg /change monitor-timeout-dc 5
    powercfg /setacvalueindex SCHEME_CURRENT SUB_NONE $lockGuid 1
    powercfg /setdcvalueindex SCHEME_CURRENT SUB_NONE $lockGuid 1
    powercfg /setactive SCHEME_CURRENT
    Set-ItemProperty $desktop -Name ScreenSaveActive -Value '1'
    Set-ItemProperty $desktop -Name ScreenSaverIsSecure -Value '1'
    Remove-ItemProperty $policy -Name InactivityTimeoutSecs -ErrorAction SilentlyContinue
    Write-Host 'Reverted to typical sleep/lock defaults.'
} else {
    powercfg /change standby-timeout-ac 0; powercfg /change standby-timeout-dc 0
    powercfg /change monitor-timeout-ac 0; powercfg /change monitor-timeout-dc 0
    powercfg /change hibernate-timeout-ac 0; powercfg /change hibernate-timeout-dc 0
    powercfg /setacvalueindex SCHEME_CURRENT SUB_NONE $lockGuid 0
    powercfg /setdcvalueindex SCHEME_CURRENT SUB_NONE $lockGuid 0
    powercfg /setactive SCHEME_CURRENT
    Set-ItemProperty $desktop -Name ScreenSaveActive -Value '0'
    Set-ItemProperty $desktop -Name ScreenSaverIsSecure -Value '0'
    Set-ItemProperty $policy -Name InactivityTimeoutSecs -Value 0 -Type DWord
    Write-Host 'Done: this PC will not sleep, turn off its screen, or lock on its own.'
    Write-Host 'A manual Win+L still locks it; avoid pressing it on the remote PC.'
    Write-Host 'Undo any time with:  .\keep-awake.ps1 -Revert'
}
