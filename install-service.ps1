#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [string]$Source = (Join-Path $PSScriptRoot 'build_all\svc\dist\AnyKirSvc'),
    [string]$HostConfig = (Join-Path $env:APPDATA 'AnyKir\host.json'),
    [ValidateRange(1, 65535)][int]$Port = 7001,
    [string]$Relay = '',
    [string]$RelayKey = ''
)
$ErrorActionPreference = 'Stop'

function Invoke-ScChecked([string[]]$Arguments) {
    & "$env:SystemRoot\System32\sc.exe" @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Service operation failed (exit $LASTEXITCODE)." }
}

function Assert-NoReparsePoint([string]$Path) {
    $itemPath = [IO.Path]::GetFullPath($Path)
    while ($itemPath) {
        if (Test-Path -LiteralPath $itemPath) {
            $item = Get-Item -LiteralPath $itemPath -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Refusing a service path containing a reparse point: $itemPath"
            }
        }
        $parentPath = Split-Path -Parent $itemPath
        if ($parentPath -eq $itemPath) { break }
        $itemPath = $parentPath
    }
}

function Set-ProtectedDirectory([string]$Path, [bool]$AllowRead) {
    Assert-NoReparsePoint $Path
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $administrators = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    $acl.SetOwner($administrators)
    foreach ($sid in @('S-1-5-18', 'S-1-5-32-544')) {
        $identity = [Security.Principal.SecurityIdentifier]::new($sid)
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
    }
    if ($AllowRead) {
        $users = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-545')
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $users, 'ReadAndExecute', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Get-RandomToken([int]$Length) {
    $bytes = New-Object byte[] $Length
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return [Convert]::ToBase64String($bytes)
}

$sourceDir = (Resolve-Path -LiteralPath $Source).Path
$sourceExe = Join-Path $sourceDir 'AnyKirSvc.exe'
if (-not (Test-Path -LiteralPath $sourceExe)) {
    throw "Service build not found. Run build.bat first (expected AnyKirSvc.exe in $sourceDir)."
}
$installDir = Join-Path ([Environment]::GetFolderPath('ProgramFiles')) 'AnyKirService'
$dataDir = Join-Path ([Environment]::GetFolderPath('CommonApplicationData')) 'AnyKirService'
$installedExe = Join-Path $installDir 'AnyKirSvc.exe'
Assert-NoReparsePoint $installDir
Assert-NoReparsePoint $dataDir
if ($sourceDir -eq $installDir) { throw 'Install from a newly built service folder, not the installed service directory.' }

$service = Get-Service -Name AnyKir -ErrorAction SilentlyContinue
if ($service) { Stop-Service -Name AnyKir -Force; $service.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30)) }

Set-ProtectedDirectory $installDir $true
Set-ProtectedDirectory $dataDir $false
Assert-NoReparsePoint $installedExe
Assert-NoReparsePoint (Join-Path $dataDir 'host.json')
Get-ChildItem -LiteralPath $installDir -Force | Remove-Item -Recurse -Force
Copy-Item -Path (Join-Path $sourceDir '*') -Destination $installDir -Recurse -Force

$configPath = Join-Path $dataDir 'host.json'
$existing = $null
if (Test-Path -LiteralPath $configPath) {
    $existing = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
} elseif (Test-Path -LiteralPath $HostConfig) {
    $existing = Get-Content -LiteralPath $HostConfig -Raw | ConvertFrom-Json
}
$cfg = [ordered]@{
    id = ([string]([BitConverter]::ToUInt32([Convert]::FromBase64String((Get-RandomToken 4)), 0) % 900000000 + 100000000))
    secret = (Get-RandomToken 32)
    password = (Get-RandomToken 18)
    relay = $Relay
    relay_key = $RelayKey
    listen = $Port
    allow_input = $true
    allow_update = $false
    allow_files = $false
    allow_clipboard = $false
    follow = ''
}
if ($existing -and $existing.follow) { $cfg.follow = [string]$existing.follow }
if ($existing) {
    if ([string]$existing.id -match '^\d{6,12}$') { $cfg.id = [string]$existing.id }
    if (([string]$existing.secret).Length -ge 16) { $cfg.secret = [string]$existing.secret }
    if (([string]$existing.password).Length -ge 8) { $cfg.password = [string]$existing.password }
    if (-not $PSBoundParameters.ContainsKey('Relay')) { $cfg.relay = [string]$existing.relay }
    if (-not $PSBoundParameters.ContainsKey('RelayKey')) { $cfg.relay_key = [string]$existing.relay_key }
    if ($existing.allow_input -eq $false) { $cfg.allow_input = $false }
}
if ($PSBoundParameters.ContainsKey('HostConfig') -and (Test-Path -LiteralPath $HostConfig)) {
    $hostCfg = Get-Content -LiteralPath $HostConfig -Raw | ConvertFrom-Json
    $cfg.follow = [IO.Path]::GetFullPath($HostConfig)
    if ([string]$hostCfg.id -match '^\d{6,12}$') { $cfg.id = [string]$hostCfg.id }
    if (([string]$hostCfg.secret).Length -ge 16) { $cfg.secret = [string]$hostCfg.secret }
    if (([string]$hostCfg.password).Length -ge 8) { $cfg.password = [string]$hostCfg.password }
    if ($hostCfg.allow_input -is [bool]) { $cfg.allow_input = $hostCfg.allow_input }
    if (-not $PSBoundParameters.ContainsKey('Relay')) { $cfg.relay = [string]$hostCfg.relay }
    if (-not $PSBoundParameters.ContainsKey('RelayKey')) { $cfg.relay_key = [string]$hostCfg.relay_key }
    if (-not $PSBoundParameters.ContainsKey('Port') -and [int]$hostCfg.listen -ge 1 -and [int]$hostCfg.listen -le 65535) {
        $cfg.listen = [int]$hostCfg.listen; $Port = $cfg.listen
    }
}
[IO.File]::WriteAllText($configPath, ($cfg | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
foreach ($path in @($installedExe, $configPath)) {
    $fileAcl = [Security.AccessControl.FileSecurity]::new()
    $fileAcl.SetAccessRuleProtection($false, $false)
    $fileAcl.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
    Set-Acl -LiteralPath $path -AclObject $fileAcl
}
$binPath = '"' + $installedExe + '" --service'
# New-Service, not sc.exe: PowerShell 5.1 mangles the quoted binPath it passes to sc.exe.
if ($service) {
    Invoke-ScChecked @('delete', 'AnyKir')
    Start-Sleep -Seconds 1
}
New-Service -Name AnyKir -BinaryPathName $binPath -DisplayName 'AnyKir administrator access' `
    -Description 'Remote screen and input for administrator windows and UAC prompts.' `
    -StartupType Automatic | Out-Null
Invoke-ScChecked @('failure', 'AnyKir', 'reset=', '86400', 'actions=', 'restart/5000/restart/15000/restart/60000')
Start-Service -Name AnyKir
Write-Host 'AnyKir administrator-access service installed and started.'
Write-Host "Connect to this PC on port $Port using your usual address. Relay ID: $($cfg.id)"
Write-Host "Password: $($cfg.password)"
Write-Host 'The service controls the signed-in physical console session. Close the portable host to avoid a port conflict.'
Write-Host 'File transfer, clipboard sync, and automatic updates are disabled in service mode.'
Write-Host 'Stop access: Stop-Service AnyKir. Uninstall: AnyKir.exe --uninstall-service'
