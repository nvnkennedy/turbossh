<#
.SYNOPSIS
    One-shot OpenSSH Server setup for a Windows machine, fully OFFLINE.
    Installs from the OpenSSH ZIP bundled with the turbossh package
    (chosen by CPU architecture), starts sshd, opens the firewall, and
    optionally pip-installs the package. Self-elevates to Administrator.

.DESCRIPTION
    No Windows Update / Features-on-Demand and no internet required - this
    avoids the Add-WindowsCapability hang on locked-down corporate networks.
    The matching ZIP (ARM64 / Win64 / Win32) ships inside the package under
    .\openssh\ next to this script.

.PARAMETER InstallPip
    Also run `pip install -U turbossh` (needs Python+pip on PATH).

.PARAMETER Port
    SSH port to open in the firewall (default 22).

.PARAMETER ZipPath
    Use a specific OpenSSH ZIP instead of the bundled one (override).

.PARAMETER Force
    Reinstall even if sshd already exists.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\setup_openssh_server.ps1
.EXAMPLE
    turbossh-setup --InstallPip
#>

param(
    [switch]$InstallPip,
    [int]$Port = 22,
    [string]$ZipPath,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# --- 1. Self-elevate to Administrator -------------------------------------- #
$isAdmin = ([Security.Principal.WindowsPrincipal]`
    [Security.Principal.WindowsIdentity]::GetCurrent()`
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host "Elevating to Administrator..." -ForegroundColor Yellow
    $argList = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Port $Port"
    if ($InstallPip) { $argList += " -InstallPip" }
    if ($Force)      { $argList += " -Force" }
    if ($ZipPath)    { $argList += " -ZipPath `"$ZipPath`"" }
    Start-Process powershell -Verb RunAs -ArgumentList $argList
    exit
}

Write-Host "=== OpenSSH Server setup (Administrator, offline) ===" -ForegroundColor Cyan

function Test-SshdPresent {
    return [bool](Get-Service -Name sshd -ErrorAction SilentlyContinue)
}

# --- 2. Pick the right bundled ZIP by CPU architecture --------------------- #
function Get-OSArch {
    try {
        $a = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
        if ($a) { return $a }
    } catch {}
    $p = $env:PROCESSOR_ARCHITEW6432
    if (-not $p) { $p = $env:PROCESSOR_ARCHITECTURE }
    switch ($p) {
        'AMD64' { 'X64' } 'ARM64' { 'Arm64' } 'x86' { 'X86' } default { $p }
    }
}

function Resolve-BundledZip {
    $arch = Get-OSArch
    switch -Regex ($arch) {
        'Arm64' { $name = 'OpenSSH-ARM64.zip' }
        'X64'   { $name = 'OpenSSH-Win64.zip' }
        'X86'   { $name = 'OpenSSH-Win32.zip' }
        default { throw "Unsupported architecture: $arch" }
    }
    Write-Host ("  Detected architecture: {0} -> {1}" -f $arch, $name)
    return (Join-Path $PSScriptRoot ("openssh\" + $name))
}

function Send-EnvironmentChanged {
    # Tell running programs (Explorer, hence new consoles) that the environment
    # changed, as SetEnvironmentVariable does. Best effort.
    try {
        if (-not ('TurboSsh.EnvNotify' -as [type])) {
            Add-Type -Namespace TurboSsh -Name EnvNotify -MemberDefinition @'
[DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint msg, UIntPtr wParam,
    string lParam, uint flags, uint timeout, out UIntPtr result);
'@
        }
        $res = [UIntPtr]::Zero
        # HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG, 5 s
        [void][TurboSsh.EnvNotify]::SendMessageTimeout([IntPtr]0xffff, 0x1A,
            [UIntPtr]::Zero, 'Environment', 2, 5000, [ref]$res)
    } catch {}
}

function Add-MachinePath([string]$Dir) {
    # Read and write the registry value itself. [Environment]::GetEnvironmentVariable
    # returns PATH with every %VAR% already expanded and SetEnvironmentVariable
    # writes it back as REG_SZ: the system PATH lost its REG_EXPAND_SZ type and
    # entries like %SystemRoot%\system32 or %JAVA_HOME%\bin were frozen for good.
    $key = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey(
        'SYSTEM\CurrentControlSet\Control\Session Manager\Environment', $true)
    if (-not $key) { throw 'cannot open the system environment registry key' }
    try {
        $raw = [string]$key.GetValue('Path', '',
            [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        $have = @($raw -split ';' | Where-Object { $_ } |
                  ForEach-Object { [Environment]::ExpandEnvironmentVariables($_).TrimEnd('\') })
        if ($have -contains $Dir.TrimEnd('\')) { return }       # already there
        $new = if ($raw.Trim()) { $raw.TrimEnd(';') + ';' + $Dir } else { $Dir }
        $key.SetValue('Path', $new, [Microsoft.Win32.RegistryValueKind]::ExpandString)
    }
    finally { $key.Close() }
    Send-EnvironmentChanged
}

# --- 3. Install OpenSSH Server from the ZIP -------------------------------- #
if ((Test-SshdPresent) -and -not $Force) {
    Write-Host "OpenSSH Server already present (use -Force to reinstall)." -ForegroundColor Green
}
else {
    $zip = if ($ZipPath) { $ZipPath } else { Resolve-BundledZip }
    if (-not (Test-Path $zip)) {
        throw "OpenSSH ZIP not found: $zip"
    }
    Write-Host "Installing OpenSSH Server from: $zip"

    $dest = Join-Path $env:ProgramFiles 'OpenSSH'
    $tmp  = Join-Path $env:TEMP ("openssh_" + [guid]::NewGuid().ToString('N'))
    try {
        Expand-Archive -Path $zip -DestinationPath $tmp -Force
        # the archive nests a single OpenSSH-XXX folder
        $srcDir = Get-ChildItem $tmp -Directory | Select-Object -First 1
        if (-not $srcDir) { $srcDir = Get-Item $tmp }

        if (-not (Test-Path $dest)) {
            New-Item -ItemType Directory -Path $dest | Out-Null
        }
        Copy-Item (Join-Path $srcDir.FullName '*') $dest -Recurse -Force
        Write-Host "  Files copied to $dest"

        $installer = Join-Path $dest 'install-sshd.ps1'
        if (-not (Test-Path $installer)) { throw "install-sshd.ps1 missing in $dest" }
        & powershell -NoProfile -ExecutionPolicy Bypass -File $installer | Out-Null
        Write-Host "  OpenSSH Server installed." -ForegroundColor Green

        # Generate host keys and fix their ACLs. Without this, ZIP installs often
        # accept the TCP connection but fail the handshake -> clients see
        # "Error reading SSH protocol banner".
        $keygen = Join-Path $dest 'ssh-keygen.exe'
        if (Test-Path $keygen) {
            & $keygen -A | Out-Null
            Write-Host "  Host keys generated." -ForegroundColor Green
        }
        $fixPerms = Join-Path $dest 'FixHostFilePermissions.ps1'
        if (Test-Path $fixPerms) {
            # NB: run in-session (NOT `powershell -File ... -Confirm:$false`, which
            # passes "$false" as a STRING and fails to bind the -Confirm switch).
            $ConfirmPreference = 'None'
            & $fixPerms -Confirm:$false | Out-Null
            Write-Host "  Host key permissions fixed." -ForegroundColor Green
        }
    }
    finally {
        if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue }
    }

    # add the install dir to the machine PATH so ssh/sshd are callable
    try { Add-MachinePath $dest }
    catch { Write-Warning "Could not add $dest to the system PATH: $($_.Exception.Message)" }
}

# --- 4. Repair host keys, (re)start the service, open the firewall --------- #
#     Runs EVERY time (even if sshd was already present) so a half-finished
#     earlier install — the usual reason for "installed but not listening" —
#     gets fixed without needing -Force.
Write-Host "Configuring sshd service and firewall..."

# locate the OpenSSH install dir (for ssh-keygen / FixHostFilePermissions)
$dest = Join-Path $env:ProgramFiles 'OpenSSH'
try {
    $svcObj = Get-CimInstance Win32_Service -Filter "Name='sshd'" -ErrorAction SilentlyContinue
    if ($svcObj -and $svcObj.PathName) {
        $exe = ($svcObj.PathName -replace '^"', '' -replace '".*$', '')
        if (Test-Path $exe) { $dest = Split-Path $exe -Parent }
    }
} catch {}

# host keys must exist (and have tight ACLs) or sshd refuses to start / handshake
$keyDir  = Join-Path $env:ProgramData 'ssh'
$haveKey = Test-Path (Join-Path $keyDir 'ssh_host_ed25519_key')
$keygen  = Join-Path $dest 'ssh-keygen.exe'
if ((-not $haveKey) -and (Test-Path $keygen)) {
    Write-Host "  Generating missing host keys..."
    & $keygen -A | Out-Null
}
$fixPerms = Join-Path $dest 'FixHostFilePermissions.ps1'
if (Test-Path $fixPerms) {
    $ConfirmPreference = 'None'
    & $fixPerms -Confirm:$false | Out-Null      # in-session: -Confirm binds correctly
    Write-Host "  Host key permissions ensured." -ForegroundColor Green
}

# Make sshd actually LISTEN on the requested port (patch sshd_config). The
# firewall rule alone doesn't change what sshd binds to, so a non-default -Port
# would otherwise still listen on 22 and the verification below would report FAIL.
$sshdCfg = Join-Path $env:ProgramData 'ssh\sshd_config'
if (Test-Path $sshdCfg) {
    $cfgLines = Get-Content $sshdCfg
    if ($cfgLines -match '^\s*#?\s*Port\s+\d+') {
        $cfgLines = $cfgLines -replace '^\s*#?\s*Port\s+\d+', "Port $Port"
    } else {
        $cfgLines += "Port $Port"
    }
    Set-Content -Path $sshdCfg -Value $cfgLines -Encoding ascii
    Write-Host "  sshd_config set to listen on port $Port." -ForegroundColor Green
}

Set-Service -Name sshd -StartupType Automatic
try { Restart-Service sshd -ErrorAction Stop }
catch { try { Start-Service sshd } catch { Write-Warning "Start-Service sshd failed: $($_.Exception.Message)" } }

$ruleName = "OpenSSH-Server-In-TCP"
$rule = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
if (-not $rule) {
    New-NetFirewallRule -Name $ruleName -DisplayName 'OpenSSH Server (sshd)' `
        -Enabled True -Direction Inbound -Protocol TCP -Action Allow `
        -LocalPort $Port -Profile Any | Out-Null
    Write-Host "  Firewall rule added for TCP $Port (all profiles)." -ForegroundColor Green
}
else {
    Enable-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
    Set-NetFirewallRule -Name $ruleName -Enabled True -Profile Any -ErrorAction SilentlyContinue
    try { $rule | Get-NetFirewallPortFilter |
              Set-NetFirewallPortFilter -Protocol TCP -LocalPort $Port -ErrorAction SilentlyContinue } catch {}
    Write-Host "  Firewall rule ensured (enabled, all profiles, TCP $Port)." -ForegroundColor Green
}

# --- 5. Optional: pip install the package ---------------------------------- #
if ($InstallPip) {
    Write-Host "Installing turbossh (pip)..."
    try {
        python -m pip install --upgrade pip | Out-Null
        python -m pip install -U turbossh
        Write-Host "  turbossh installed." -ForegroundColor Green
    }
    catch {
        Write-Warning "pip install failed: $($_.Exception.Message)"
    }
}

# --- 6. Verify + write a result the GUI can read --------------------------- #
Write-Host "`n=== Verification ===" -ForegroundColor Cyan
$svc = Get-Service sshd -ErrorAction SilentlyContinue
$svcStatus = if ($svc) { "$($svc.Status)" } else { 'Missing' }
Write-Host ("sshd status : {0}" -f $svcStatus)
$test = Test-NetConnection localhost -Port $Port -WarningAction SilentlyContinue
$listening = [bool]$test.TcpTestSucceeded
Write-Host ("port {0} open : {1}" -f $Port, $listening)

$ok = ($svcStatus -eq 'Running' -and $listening)

# Write a small result file the GUI polls (ProgramData is readable by all users,
# so it works even when this script runs elevated as a different account).
#
# But any user may also CREATE folders in ProgramData: one could make 'turbossh'
# a junction, or plant a link inside it, and have this elevated script overwrite
# a file of their choosing. So the result only goes into a folder that is ours:
# a plain folder (no reparse point), owned by Administrators or SYSTEM, whose
# entries let nobody else change it. Anything else at that name is renamed aside
# (renaming moves a link itself, never its target) and the folder is created
# afresh with that ACL already in place. The file is then replaced, not written
# through: deleting a planted link deletes the link.
$script:Admins = New-Object Security.Principal.SecurityIdentifier 'S-1-5-32-544'
$script:System = New-Object Security.Principal.SecurityIdentifier 'S-1-5-18'
$script:Users  = New-Object Security.Principal.SecurityIdentifier 'S-1-5-32-545'

function Test-AdminOnlyFolder([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if (-not $item -or -not $item.PSIsContainer) { return $false }
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { return $false }
    try { $sd = $item.GetAccessControl() } catch { return $false }   # its owner locked us out
    $owner = $sd.GetOwner([Security.Principal.SecurityIdentifier])
    if (-not ($owner.Equals($script:Admins) -or $owner.Equals($script:System))) { return $false }
    if (-not $sd.AreAccessRulesProtected) { return $false }
    # write data / append / attributes, delete (child), change permissions, take
    # ownership, and the generic all / write bits
    $writeMask = 0x500D0156
    foreach ($rule in $sd.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        $sid = $rule.IdentityReference
        if ($rule.AccessControlType -eq 'Allow' -and ([int64]$rule.FileSystemRights -band $writeMask) -and
            -not ($sid.Equals($script:Admins) -or $sid.Equals($script:System))) { return $false }
    }
    return $true
}

function Write-SetupResult([string[]]$Lines) {
    $dir = Join-Path $env:ProgramData 'turbossh'
    if (-not (Test-AdminOnlyFolder $dir)) {
        if (Get-Item -LiteralPath $dir -Force -ErrorAction SilentlyContinue) {
            # an older TurboSSH's folder (Users could add files), or something planted
            $aside = $dir + '.old-' + (Get-Date -Format 'yyyyMMddHHmmss')
            Rename-Item -LiteralPath $dir -NewName (Split-Path $aside -Leaf) -Force
            Write-Host "  Moved the existing $dir aside to $aside." -ForegroundColor Yellow
        }
        $acl = New-Object Security.AccessControl.DirectorySecurity
        $acl.SetOwner($script:Admins)
        $acl.SetAccessRuleProtection($true, $false)
        $inherit = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
        $none = [Security.AccessControl.PropagationFlags]::None
        foreach ($entry in @(@($script:Admins, 'FullControl'), @($script:System, 'FullControl'),
                             @($script:Users, 'ReadAndExecute'))) {
            $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
                $entry[0], [Security.AccessControl.FileSystemRights]$entry[1], $inherit, $none, 'Allow')))
        }
        # created with the ACL in place: there is no moment anyone else can write to it
        [void][IO.Directory]::CreateDirectory($dir, $acl)
        # if something else claimed the name in between, CreateDirectory just
        # returned it: check again rather than trust it
        if (-not (Test-AdminOnlyFolder $dir)) { throw "$dir is not a folder only Administrators control" }
    }
    $file = Join-Path $dir 'sshd-setup-result.txt'
    if (Test-Path -LiteralPath $file) { [IO.File]::Delete($file) }
    # CreateNew: never opens (and writes through) something that is already there.
    # No BOM: the GUI reads the file as plain UTF-8.
    $stream = New-Object IO.FileStream($file, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
    try {
        $writer = New-Object IO.StreamWriter($stream, (New-Object Text.UTF8Encoding($false)))
        foreach ($line in $Lines) { $writer.WriteLine($line) }
        $writer.Flush()
    }
    finally { $stream.Dispose() }
}

try {
    Write-SetupResult @(
        ("status="    + $(if ($ok) { 'OK' } else { 'FAIL' })),
        ("sshd="      + $svcStatus),
        ("port="      + $Port),
        ("listening=" + $listening),
        ("time="      + (Get-Date -Format s))
    )
} catch {
    Write-Warning "Could not write the result file for the GUI: $($_.Exception.Message)"
}

if ($ok) {
    Write-Host "`nDone. This machine now accepts SSH on port $Port." -ForegroundColor Green
    Write-Host "This window will close in 4 seconds..."
    Start-Sleep -Seconds 4
}
else {
    Write-Warning "Setup finished but sshd is not listening on port $Port yet."
    Write-Host  "Likely a firewall / Group Policy blocking inbound $Port, or the service"
    Write-Host  "failed to start. Try:  Restart-Service sshd ;  Get-Service sshd"
    Read-Host   "`nPress Enter to close this window"
}
