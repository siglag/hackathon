param(
    [Parameter(Mandatory = $true)][System.Net.IPAddress]$WslAddress,
    [Parameter(Mandatory = $true)][System.Net.IPAddress]$LanAddress,
    [ValidateRange(1, 65535)][int]$Port = 8600
)

$ErrorActionPreference = 'Stop'
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -WslAddress {1} -LanAddress {2} -Port {3}' -f $PSCommandPath, $WslAddress, $LanAddress, $Port
    $child = Start-Process powershell.exe -Verb RunAs -ArgumentList $arguments -Wait -PassThru
    exit $child.ExitCode
}

try {
    Start-Service iphlpsvc
    & netsh interface portproxy add v4tov4 "listenaddress=$LanAddress" "listenport=$Port" "connectaddress=$WslAddress" "connectport=$Port"
    if ($LASTEXITCODE -ne 0) { throw 'Failed to configure Windows-to-WSL forwarding.' }

    $ruleName = "MedAssist-WSL-$Port"
    $rule = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
    if ($rule) {
        $rule | Set-NetFirewallRule -Enabled True -Direction Inbound -Action Allow -Profile Any
        $rule | Get-NetFirewallAddressFilter | Set-NetFirewallAddressFilter -LocalAddress "$LanAddress" -RemoteAddress LocalSubnet
        $rule | Get-NetFirewallPortFilter | Set-NetFirewallPortFilter -Protocol TCP -LocalPort $Port
    } else {
        New-NetFirewallRule -Name $ruleName -DisplayName "MedAssist phone access ($Port)" -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -LocalAddress "$LanAddress" -RemoteAddress LocalSubnet -Profile Any | Out-Null
    }
    Write-Host "Phone URL: http://${LanAddress}:$Port"
} catch {
    Write-Error $_
    exit 1
}
