<#
.SYNOPSIS
  Oracle Cloud 东京(ap-tokyo-1) 到 PHX(us-phoenix-1) 的 AlphaPilot 自动化迁移编排脚本。

.DESCRIPTION
  该脚本从本地 Windows 控制端执行，负责：
  1. 初始化/校验 OCI API signing key 配置。
  2. 在 PHX 创建专用 VCN/公网子网/安全规则和 A1 实例。
  3. 从东京旧机打包并同步项目、配置、数据和 systemd unit 到 PHX 新机。
  4. 启动并验证 PHX 新机服务。
  5. 生成东京旧资源清单；删除旧资源必须传入确认词。

  重要边界：
  - OCI API 私钥只保存在本机，不会输出到日志。
  - 默认不会删除东京旧资源；必须使用 -Phase CleanupTokyo 且传入确认词。
  - 默认创建 alpha-pilot-phx-* 专用网络资源，避免依赖已有 PHX 网络。

.EXAMPLE
  pwsh scripts/migrate-oci-phx.ps1 -Phase InitApiKey

.EXAMPLE
  pwsh scripts/migrate-oci-phx.ps1 -Phase Provision -CompartmentId ocid1.compartment...

.EXAMPLE
  pwsh scripts/migrate-oci-phx.ps1 -Phase Migrate -CompartmentId ocid1.compartment...

.EXAMPLE
  pwsh scripts/migrate-oci-phx.ps1 -Phase CleanupInventory -CompartmentId ocid1.compartment...

.EXAMPLE
  pwsh scripts/migrate-oci-phx.ps1 -Phase CleanupTokyo -CompartmentId ocid1.compartment... -ConfirmDelete DELETE_TOKYO_QUANT_PILOT
#>

[CmdletBinding()]
param(
    [ValidateSet("Plan", "InitApiKey", "Provision", "Migrate", "Validate", "CleanupInventory", "CleanupTokyo", "All")]
    [string]$Phase = "Plan",

    [string]$OciProfile = "quant-phx",
    [string]$CompartmentId = "",
    [string]$PhxRegion = "us-phoenix-1",
    [string]$TokyoRegion = "ap-tokyo-1",

    [string]$OldHost = "193.123.167.131",
    [string]$OldUser = "ubuntu",
    [string]$NewUser = "ubuntu",
    [string]$SshKey = "$HOME\.ssh\id_ed25519",
    [string]$SshPubKey = "$HOME\.ssh\id_ed25519.pub",

    [string]$NewDisplayName = "alpha-pilot-phx",
    [string]$VcnDisplayName = "alpha-pilot-phx-vcn",
    [string]$SubnetDisplayName = "alpha-pilot-phx-subnet",
    [string]$SecurityListDisplayName = "alpha-pilot-phx-security-list",
    [string]$RouteTableDisplayName = "alpha-pilot-phx-route-table",
    [string]$InternetGatewayDisplayName = "alpha-pilot-phx-igw",
    [string]$AdminSourceCidr = "0.0.0.0/0",
    [int]$Ocpus = 4,
    [int]$MemoryInGBs = 24,
    [int]$BootVolumeSizeGB = 100,

    [string]$OciUserOcid = "",
    [string]$OciTenancyOcid = "",
    [string]$OciFingerprint = "",

    [bool]$StopOldServices = $true,
    [bool]$StartServices = $true,
    [string]$ConfirmDelete = "",

    [string]$StateDir = ".migration"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Script:RootDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Script:StatePath = Join-Path $Script:RootDir $StateDir
$Script:StateFile = Join-Path $Script:StatePath "oci-phx-migration-state.json"
$Script:InventoryFile = Join-Path $Script:StatePath "oci-tokyo-cleanup-inventory.json"
$Script:LogFile = Join-Path $Script:StatePath ("oci-phx-migration-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
$Script:ServiceUnits = @(
    "alpha-pilot-auto-trader.service",
    "alpha-pilot-dashboard.service",
    "alpha-pilot-fastapi-server.service",
    "alpha-pilot-gateway.service",
    "alpha-pilot-watchdog.service",
    "alpha-pilot-auto-restart-scheduler.service",
    "alpha-pilot-market-data.service",
    "socket-proxy-alpha-pilot.service"
)

New-Item -ItemType Directory -Force -Path $Script:StatePath | Out-Null
if (-not $env:PYTHONIOENCODING) {
    $env:PYTHONIOENCODING = "utf-8"
}
if (-not $env:OCI_CLI_SUPPRESS_FILE_PERMISSIONS_WARNING) {
    $env:OCI_CLI_SUPPRESS_FILE_PERMISSIONS_WARNING = "True"
}
if (-not $env:SUPPRESS_LABEL_WARNING) {
    $env:SUPPRESS_LABEL_WARNING = "True"
}

function Write-Step {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    Add-Content -Path $Script:LogFile -Value $line
}

function Write-Warn {
    param([string]$Message)
    $line = "[{0}] WARN {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Warning $Message
    Add-Content -Path $Script:LogFile -Value $line
}

function ConvertTo-PlainText {
    param([object[]]$Output)
    if ($null -eq $Output) {
        return ""
    }
    return (($Output | Where-Object { $null -ne $_ } | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine)
}

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$AllowFailure,
        [switch]$Quiet
    )
    if (-not $Quiet) {
        Write-Step ("> {0} {1}" -f $Exe, ($Arguments -join " "))
    }
    $output = & $Exe @Arguments 2>&1
    $code = $LASTEXITCODE
    $text = ConvertTo-PlainText $output
    if ($text -and -not $Quiet) {
        Add-Content -Path $Script:LogFile -Value $text
    }
    if ($code -ne 0 -and -not $AllowFailure) {
        throw "Command failed ($code): $Exe $($Arguments -join ' ')`n$text"
    }
    return @{
        Code = $code
        Text = $text
    }
}

function Invoke-OciText {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$Region = "",
        [switch]$AllowFailure
    )
    $args = @($Arguments)
    $args += @("--profile", $OciProfile)
    if ($Region) {
        $args += @("--region", $Region)
    }
    $result = Invoke-Native -Exe "oci" -Arguments $args -AllowFailure:$AllowFailure
    return $result.Text
}

function Invoke-OciJson {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$Region = "",
        [switch]$AllowFailure
    )
    $text = Invoke-OciText -Arguments $Arguments -Region $Region -AllowFailure:$AllowFailure
    if ([string]::IsNullOrWhiteSpace($text)) {
        return [pscustomobject]@{ data = @() }
    }
    $lines = $text -split "\r?\n"
    $startLine = -1
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $line = $lines[$i].TrimStart()
        if ($line.StartsWith("{") -or $line.StartsWith("[")) {
            $startLine = $i
            break
        }
    }
    if ($startLine -lt 0) {
        throw "OCI 命令未返回可解析 JSON:`n$text"
    }
    $candidateLines = $lines[$startLine..($lines.Count - 1)]
    $endLine = $candidateLines.Count - 1
    for ($i = $candidateLines.Count - 1; $i -ge 0; $i--) {
        $line = $candidateLines[$i].TrimEnd()
        if ($line.EndsWith("}") -or $line.EndsWith("]")) {
            $endLine = $i
            break
        }
    }
    $jsonText = ($candidateLines[0..$endLine] -join [Environment]::NewLine)
    return $jsonText | ConvertFrom-Json
}

function Test-CommandExists {
    param([string]$Name)
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Assert-Command {
    param([string]$Name, [string]$InstallHint)
    if (-not (Test-CommandExists $Name)) {
        throw "缺少命令: $Name。$InstallHint"
    }
}

function Assert-File {
    param([string]$Path, [string]$Hint)
    if (-not (Test-Path $Path)) {
        throw "缺少文件: $Path。$Hint"
    }
}

function ConvertTo-Pem {
    param(
        [Parameter(Mandatory = $true)][byte[]]$DerBytes,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $base64 = [Convert]::ToBase64String($DerBytes)
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("-----BEGIN $Label-----")
    for ($i = 0; $i -lt $base64.Length; $i += 64) {
        $length = [Math]::Min(64, $base64.Length - $i)
        $lines.Add($base64.Substring($i, $length))
    }
    $lines.Add("-----END $Label-----")
    return ($lines -join [Environment]::NewLine) + [Environment]::NewLine
}

function New-OciApiKeyWithDotNet {
    param(
        [Parameter(Mandatory = $true)][string]$PrivateKeyPath,
        [Parameter(Mandatory = $true)][string]$PublicKeyPath
    )
    $rsa = [System.Security.Cryptography.RSA]::Create(2048)
    try {
        $privatePem = ConvertTo-Pem -DerBytes $rsa.ExportRSAPrivateKey() -Label "RSA PRIVATE KEY"
        $publicPem = ConvertTo-Pem -DerBytes $rsa.ExportSubjectPublicKeyInfo() -Label "PUBLIC KEY"
        $privatePem | Set-Content -Encoding ASCII -NoNewline -Path $PrivateKeyPath
        $publicPem | Set-Content -Encoding ASCII -NoNewline -Path $PublicKeyPath
    } finally {
        $rsa.Dispose()
    }
}

function Assert-Compartment {
    if ([string]::IsNullOrWhiteSpace($CompartmentId)) {
        throw "需要传入 -CompartmentId，例如 tenancy OCID 或项目 compartment OCID。"
    }
}

function Save-State {
    param([hashtable]$Patch)
    $state = @{}
    if (Test-Path $Script:StateFile) {
        $raw = Get-Content -Raw -Path $Script:StateFile
        if (-not [string]::IsNullOrWhiteSpace($raw)) {
            $obj = $raw | ConvertFrom-Json
            foreach ($p in $obj.PSObject.Properties) {
                $state[$p.Name] = $p.Value
            }
        }
    }
    foreach ($key in $Patch.Keys) {
        $state[$key] = $Patch[$key]
    }
    $state["updatedAt"] = (Get-Date).ToString("o")
    $state | ConvertTo-Json -Depth 12 | Set-Content -Encoding UTF8 -Path $Script:StateFile
}

function Load-State {
    if (-not (Test-Path $Script:StateFile)) {
        return $null
    }
    $raw = Get-Content -Raw -Path $Script:StateFile
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return $null
    }
    return $raw | ConvertFrom-Json
}

function ConvertTo-JsonArg {
    param([object]$Value)
    return (ConvertTo-Json -InputObject $Value -Compress -Depth 12)
}

function Invoke-Ssh {
    param(
        [Parameter(Mandatory = $true)][string]$HostName,
        [Parameter(Mandatory = $true)][string]$User,
        [Parameter(Mandatory = $true)][string]$Command,
        [switch]$AllowFailure,
        [switch]$Quiet
    )
    $target = "{0}@{1}" -f $User, $HostName
    $args = @(
        "-i", $SshKey,
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ServerAliveInterval=30",
        $target
    )
    if (-not $Quiet) {
        Write-Step ("> ssh {0} bash -lc <base64-script>" -f $target)
    }
    $normalizedCommand = $Command -replace "`r`n", "`n"
    $normalizedCommand = $normalizedCommand -replace "`r", "`n"
    $encodedCommand = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($normalizedCommand))
    $remoteCommand = "bash -lc `"printf '%s' '$encodedCommand' | base64 -d | bash -s`""
    $output = & ssh @args $remoteCommand 2>&1
    $code = $LASTEXITCODE
    $text = ConvertTo-PlainText $output
    if ($text -and -not $Quiet) {
        Add-Content -Path $Script:LogFile -Value $text
    }
    if ($code -ne 0 -and -not $AllowFailure) {
        throw "SSH command failed ($code): $target`n$text"
    }
    return @{
        Code = $code
        Text = $text
    }
}

function Invoke-Scp {
    param(
        [Parameter(Mandatory = $true)][string[]]$ScpArgs,
        [switch]$AllowFailure
    )
    $args = @(
        "-i", $SshKey,
        "-o", "StrictHostKeyChecking=accept-new"
    ) + $ScpArgs
    return Invoke-Native -Exe "scp" -Arguments $args -AllowFailure:$AllowFailure
}

function Test-SshReady {
    param([string]$HostName, [string]$User)
    $result = Invoke-Ssh -HostName $HostName -User $User -Command "echo ssh-ok" -AllowFailure -Quiet
    return ($result.Code -eq 0 -and $result.Text -match "ssh-ok")
}

function Wait-SshReady {
    param([string]$HostName, [string]$User, [int]$TimeoutSeconds = 600)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-SshReady -HostName $HostName -User $User) {
            Write-Step "SSH 已就绪: $User@$HostName"
            return
        }
        Start-Sleep -Seconds 10
    }
    throw "等待 SSH 超时: $User@$HostName"
}

function Initialize-OciApiKey {
    $ociDir = Join-Path $HOME ".oci"
    $keyFile = Join-Path $ociDir "oci_api_key.pem"
    $pubFile = Join-Path $ociDir "oci_api_key_public.pem"
    $configFile = Join-Path $ociDir "config"

    New-Item -ItemType Directory -Force -Path $ociDir | Out-Null
    $hasOpenSsl = Test-CommandExists "openssl"

    if (-not (Test-Path $keyFile)) {
        Write-Step "生成 OCI API 私钥: $keyFile"
        if ($hasOpenSsl) {
            Invoke-Native -Exe "openssl" -Arguments @("genrsa", "-out", $keyFile, "2048") | Out-Null
        } else {
            Write-Warn "未找到 openssl，改用 PowerShell/.NET 生成 RSA PEM。"
            New-OciApiKeyWithDotNet -PrivateKeyPath $keyFile -PublicKeyPath $pubFile
        }
        $isWindowsHost = ($env:OS -eq "Windows_NT")
        $isWindowsVariable = Get-Variable -Name IsWindows -ErrorAction SilentlyContinue
        if ($isWindowsVariable) {
            $isWindowsHost = $isWindowsHost -or [bool]$isWindowsVariable.Value
        }
        if ($isWindowsHost) {
            Invoke-Native -Exe "icacls" -Arguments @($keyFile, "/inheritance:r", "/grant:r", "$env:USERNAME`:R") -AllowFailure | Out-Null
        }
    } else {
        Write-Warn "OCI API 私钥已存在，跳过生成: $keyFile"
    }

    if ($hasOpenSsl) {
        Write-Step "生成 OCI API 公钥: $pubFile"
        Invoke-Native -Exe "openssl" -Arguments @("rsa", "-pubout", "-in", $keyFile, "-out", $pubFile) | Out-Null
    } elseif (-not (Test-Path $pubFile)) {
        throw "已存在 OCI 私钥但没有公钥，且本机无 openssl。请安装 OpenSSL 后重新运行，或删除私钥让脚本用 .NET 同时重建。"
    }

    Write-Host ""
    Write-Host "请在 OCI Console 上传以下公钥文件内容到目标用户的 API Keys 页面："
    Write-Host "  $pubFile"
    Write-Host ""
    Get-Content -Path $pubFile
    Write-Host ""
    Write-Host "上传后，从 Console 复制 user OCID、tenancy OCID、fingerprint。"

    if ($OciUserOcid -and $OciTenancyOcid -and $OciFingerprint) {
        Write-Step "写入 OCI CLI profile: $OciProfile"
        $config = @"
[$OciProfile]
user=$OciUserOcid
fingerprint=$OciFingerprint
key_file=$keyFile
tenancy=$OciTenancyOcid
region=$PhxRegion
"@
        $config | Set-Content -Encoding ASCII -Path $configFile
        Write-Step "OCI config 已写入: $configFile"
    } else {
        Write-Warn "未传入 -OciUserOcid/-OciTenancyOcid/-OciFingerprint，暂不写入 config。"
        Write-Host "写入 config 后验证："
        Write-Host "  oci iam region list --profile $OciProfile"
    }
}

function Test-LocalPrerequisites {
    Write-Step "检查本地依赖"
    Assert-Command -Name "ssh" -InstallHint "请安装 OpenSSH Client。"
    Assert-Command -Name "scp" -InstallHint "请安装 OpenSSH Client。"
    Assert-Command -Name "oci" -InstallHint "请安装 OCI CLI: https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/cliinstall.htm"
    Assert-File -Path $SshKey -Hint "请确认东京服务器 SSH 私钥路径。"
    Assert-File -Path $SshPubKey -Hint "新 PHX 实例需要注入 SSH 公钥。"
    if (-not (Test-CommandExists "rsync")) {
        Write-Warn "未找到 rsync，将使用 tar+scp 两段式迁移。"
    }
}

function Test-OciProfile {
    Assert-Compartment
    Write-Step "验证 OCI profile 和两个 region 访问权限"
    Invoke-OciJson -Region $PhxRegion -Arguments @("iam", "availability-domain", "list", "--compartment-id", $CompartmentId) | Out-Null
    Invoke-OciJson -Region $TokyoRegion -Arguments @("compute", "instance", "list", "--compartment-id", $CompartmentId, "--all") | Out-Null
}

function Get-FirstAvailabilityDomain {
    $ads = Invoke-OciJson -Region $PhxRegion -Arguments @("iam", "availability-domain", "list", "--compartment-id", $CompartmentId)
    if (-not $ads -or -not $ads.data -or $ads.data.Count -lt 1) {
        throw "PHX 未返回 availability domain。"
    }
    return $ads.data[0]
}

function Find-ByDisplayName {
    param([object[]]$Items, [string]$DisplayName)
    foreach ($item in $Items) {
        if ($item."display-name" -eq $DisplayName -and $item."lifecycle-state" -ne "TERMINATED") {
            return $item
        }
    }
    return $null
}

function Ensure-PhxNetwork {
    Assert-Compartment
    Write-Step "准备 PHX 专用网络资源"

    $vcns = Invoke-OciJson -Region $PhxRegion -Arguments @("network", "vcn", "list", "--compartment-id", $CompartmentId, "--all")
    $vcn = Find-ByDisplayName -Items @($vcns.data) -DisplayName $VcnDisplayName
    if (-not $vcn) {
        $vcn = (Invoke-OciJson -Region $PhxRegion -Arguments @(
            "network", "vcn", "create",
            "--compartment-id", $CompartmentId,
            "--display-name", $VcnDisplayName,
            "--cidr-block", "10.42.0.0/16",
            "--dns-label", "quantpilot"
        )).data
    }

    $igws = Invoke-OciJson -Region $PhxRegion -Arguments @("network", "internet-gateway", "list", "--compartment-id", $CompartmentId, "--vcn-id", $vcn.id, "--all")
    $igw = Find-ByDisplayName -Items @($igws.data) -DisplayName $InternetGatewayDisplayName
    if (-not $igw) {
        $igw = (Invoke-OciJson -Region $PhxRegion -Arguments @(
            "network", "internet-gateway", "create",
            "--compartment-id", $CompartmentId,
            "--vcn-id", $vcn.id,
            "--display-name", $InternetGatewayDisplayName,
            "--is-enabled", "true"
        )).data
    }

    $routeRules = ConvertTo-JsonArg @(
        @{
            cidrBlock = "0.0.0.0/0"
            networkEntityId = $igw.id
        }
    )
    $routeTables = Invoke-OciJson -Region $PhxRegion -Arguments @("network", "route-table", "list", "--compartment-id", $CompartmentId, "--vcn-id", $vcn.id, "--all")
    $routeTable = Find-ByDisplayName -Items @($routeTables.data) -DisplayName $RouteTableDisplayName
    if (-not $routeTable) {
        $routeTable = (Invoke-OciJson -Region $PhxRegion -Arguments @(
            "network", "route-table", "create",
            "--compartment-id", $CompartmentId,
            "--vcn-id", $vcn.id,
            "--display-name", $RouteTableDisplayName,
            "--route-rules", $routeRules
        )).data
    }

    $ingressRules = @()
    foreach ($port in @(22, 5010, 5011, 5099, 9021)) {
        $ingressRules += @{
            protocol = "6"
            source = $AdminSourceCidr
            tcpOptions = @{
                destinationPortRange = @{
                    min = $port
                    max = $port
                }
            }
        }
    }
    $egressRules = @(
        @{
            protocol = "all"
            destination = "0.0.0.0/0"
        }
    )
    $securityLists = Invoke-OciJson -Region $PhxRegion -Arguments @("network", "security-list", "list", "--compartment-id", $CompartmentId, "--vcn-id", $vcn.id, "--all")
    $securityList = Find-ByDisplayName -Items @($securityLists.data) -DisplayName $SecurityListDisplayName
    if (-not $securityList) {
        $securityList = (Invoke-OciJson -Region $PhxRegion -Arguments @(
            "network", "security-list", "create",
            "--compartment-id", $CompartmentId,
            "--vcn-id", $vcn.id,
            "--display-name", $SecurityListDisplayName,
            "--ingress-security-rules", (ConvertTo-JsonArg $ingressRules),
            "--egress-security-rules", (ConvertTo-JsonArg $egressRules)
        )).data
    }

    $subnets = Invoke-OciJson -Region $PhxRegion -Arguments @("network", "subnet", "list", "--compartment-id", $CompartmentId, "--vcn-id", $vcn.id, "--all")
    $subnet = Find-ByDisplayName -Items @($subnets.data) -DisplayName $SubnetDisplayName
    if (-not $subnet) {
        $securityListIds = ConvertTo-JsonArg @($securityList.id)
        $subnet = (Invoke-OciJson -Region $PhxRegion -Arguments @(
            "network", "subnet", "create",
            "--compartment-id", $CompartmentId,
            "--vcn-id", $vcn.id,
            "--display-name", $SubnetDisplayName,
            "--cidr-block", "10.42.1.0/24",
            "--dns-label", "quantphx",
            "--route-table-id", $routeTable.id,
            "--security-list-ids", $securityListIds,
            "--prohibit-public-ip-on-vnic", "false"
        )).data
    }

    Save-State @{
        phxVcnId = $vcn.id
        phxSubnetId = $subnet.id
        phxInternetGatewayId = $igw.id
        phxRouteTableId = $routeTable.id
        phxSecurityListId = $securityList.id
    }

    return @{
        Vcn = $vcn
        Subnet = $subnet
    }
}

function Get-UbuntuArmImageId {
    Write-Step "查找 Ubuntu 24.04 ARM64 平台镜像"
    $images = Invoke-OciJson -Region $PhxRegion -Arguments @(
        "compute", "image", "list",
        "--compartment-id", $CompartmentId,
        "--operating-system", "Canonical Ubuntu",
        "--operating-system-version", "24.04",
        "--shape", "VM.Standard.A1.Flex",
        "--sort-by", "TIMECREATED",
        "--sort-order", "DESC",
        "--all"
    )
    if (-not $images -or -not $images.data -or $images.data.Count -lt 1) {
        throw "未找到 Ubuntu 24.04 / VM.Standard.A1.Flex 平台镜像。"
    }
    return $images.data[0].id
}

function Get-ExistingPhxInstance {
    $instances = Invoke-OciJson -Region $PhxRegion -Arguments @("compute", "instance", "list", "--compartment-id", $CompartmentId, "--all")
    return Find-ByDisplayName -Items @($instances.data) -DisplayName $NewDisplayName
}

function Wait-InstanceRunning {
    param([string]$InstanceId, [int]$TimeoutSeconds = 900)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $instance = (Invoke-OciJson -Region $PhxRegion -Arguments @("compute", "instance", "get", "--instance-id", $InstanceId)).data
        Write-Step ("实例状态: {0}" -f $instance."lifecycle-state")
        if ($instance."lifecycle-state" -eq "RUNNING") {
            return $instance
        }
        Start-Sleep -Seconds 15
    }
    throw "等待实例 RUNNING 超时: $InstanceId"
}

function Get-InstancePublicIp {
    param([string]$InstanceId)
    $attachments = Invoke-OciJson -Region $PhxRegion -Arguments @("compute", "vnic-attachment", "list", "--compartment-id", $CompartmentId, "--instance-id", $InstanceId, "--all")
    foreach ($attachment in $attachments.data) {
        if ($attachment."lifecycle-state" -eq "ATTACHED") {
            $vnic = (Invoke-OciJson -Region $PhxRegion -Arguments @("network", "vnic", "get", "--vnic-id", $attachment."vnic-id")).data
            if ($vnic."public-ip") {
                return $vnic."public-ip"
            }
        }
    }
    throw "未找到实例公网 IP: $InstanceId"
}

function New-PhxInstance {
    Assert-Compartment
    Assert-File -Path $SshPubKey -Hint "新实例需要 SSH 公钥。"

    $existing = Get-ExistingPhxInstance
    if ($existing) {
        Write-Warn "PHX 已存在同名实例，复用: $($existing.id)"
        $instance = Wait-InstanceRunning -InstanceId $existing.id
    } else {
        $network = Ensure-PhxNetwork
        $ad = Get-FirstAvailabilityDomain
        $imageId = Get-UbuntuArmImageId
        $sshKeyText = (Get-Content -Raw -Path $SshPubKey).Trim()
        $metadata = ConvertTo-JsonArg @{ ssh_authorized_keys = $sshKeyText }
        $shapeConfig = ConvertTo-JsonArg @{
            ocpus = $Ocpus
            memoryInGBs = $MemoryInGBs
        }

        Write-Step "创建 PHX A1 实例: $NewDisplayName"
        $instance = (Invoke-OciJson -Region $PhxRegion -Arguments @(
            "compute", "instance", "launch",
            "--availability-domain", $ad.name,
            "--compartment-id", $CompartmentId,
            "--shape", "VM.Standard.A1.Flex",
            "--display-name", $NewDisplayName,
            "--image-id", $imageId,
            "--subnet-id", $network.Subnet.id,
            "--assign-public-ip", "true",
            "--shape-config", $shapeConfig,
            "--metadata", $metadata,
            "--boot-volume-size-in-gbs", "$BootVolumeSizeGB"
        )).data
        $instance = Wait-InstanceRunning -InstanceId $instance.id
    }

    $publicIp = Get-InstancePublicIp -InstanceId $instance.id
    Save-State @{
        phxInstanceId = $instance.id
        phxPublicIp = $publicIp
        phxRegion = $PhxRegion
        newUser = $NewUser
    }
    Write-Step "PHX 实例公网 IP: $publicIp"
    Wait-SshReady -HostName $publicIp -User $NewUser
}

function Get-NewHostFromState {
    $state = Load-State
    if (-not $state -or -not $state.phxPublicIp) {
        throw "未找到 PHX 公网 IP。请先运行 -Phase Provision，或检查 $Script:StateFile。"
    }
    return $state.phxPublicIp
}

function New-RemoteMigrationArchives {
    $stopBlock = ""
    if ($StopOldServices) {
        $units = ($Script:ServiceUnits -join " ")
        $stopBlock = "sudo systemctl stop $units 2>/dev/null || true"
    }

    $remote = @"
set -euo pipefail
$stopBlock
rm -rf /tmp/alpha-pilot-migration
mkdir -p /tmp/alpha-pilot-migration
sudo tar -C /etc/systemd/system -czf /tmp/alpha-pilot-migration/systemd.tgz --ignore-failed-read alpha-pilot*.service socket-proxy-alpha-pilot* 2>/tmp/alpha-pilot-migration/systemd-tar.err || true
tar -C "`$HOME" -czf /tmp/alpha-pilot-migration/project.tgz \
  --exclude='projects/alpha-pilot/venv-quant' \
  --exclude='projects/alpha-pilot/venv' \
  --exclude='projects/alpha-pilot/.venv' \
  --exclude='projects/alpha-pilot/**/__pycache__' \
  --exclude='projects/alpha-pilot/data/auto_trader.lock' \
  --exclude='projects/alpha-pilot/.git' \
  --exclude='.hermes/logs' \
  projects/alpha-pilot .hermes .bashrc || [ "`$?" -eq 1 ]
ls -lh /tmp/alpha-pilot-migration
"@
    Invoke-Ssh -HostName $OldHost -User $OldUser -Command $remote | Out-Null
}

function Copy-ArchivesToNewHost {
    param([string]$NewHost)
    $localStage = Join-Path $Script:StatePath "transfer"
    New-Item -ItemType Directory -Force -Path $localStage | Out-Null

    Write-Step "从东京旧机下载迁移包"
    Invoke-Scp -ScpArgs @(
        ("{0}@{1}:/tmp/alpha-pilot-migration/project.tgz" -f $OldUser, $OldHost),
        (Join-Path $localStage "project.tgz")
    ) | Out-Null
    Invoke-Scp -ScpArgs @(
        ("{0}@{1}:/tmp/alpha-pilot-migration/systemd.tgz" -f $OldUser, $OldHost),
        (Join-Path $localStage "systemd.tgz")
    ) -AllowFailure | Out-Null

    Write-Step "上传迁移包到 PHX 新机"
    Invoke-Ssh -HostName $NewHost -User $NewUser -Command "mkdir -p /tmp/alpha-pilot-migration" | Out-Null
    Invoke-Scp -ScpArgs @(
        (Join-Path $localStage "project.tgz"),
        ("{0}@{1}:/tmp/alpha-pilot-migration/project.tgz" -f $NewUser, $NewHost)
    ) | Out-Null
    if (Test-Path (Join-Path $localStage "systemd.tgz")) {
        Invoke-Scp -ScpArgs @(
            (Join-Path $localStage "systemd.tgz"),
            ("{0}@{1}:/tmp/alpha-pilot-migration/systemd.tgz" -f $NewUser, $NewHost)
        ) | Out-Null
    }
}

function Install-NewHost {
    param([string]$NewHost)
    $startBlock = ""
    if ($StartServices) {
        $units = ($Script:ServiceUnits -join " ")
        $startBlock = "for unit in $units; do if systemctl list-unit-files ""`$unit"" --no-pager 2>/dev/null | grep -q ""`$unit""; then sudo systemctl enable --now ""`$unit"" || true; fi; done"
    }

    $remote = @"
set -euo pipefail
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3.12-venv python3-pip git sqlite3 curl jq rsync
tar -C "`$HOME" -xzf /tmp/alpha-pilot-migration/project.tgz
if [ -s /tmp/alpha-pilot-migration/systemd.tgz ]; then
  sudo tar -C /etc/systemd/system -xzf /tmp/alpha-pilot-migration/systemd.tgz || true
fi
sudo chown -R ${NewUser}:${NewUser} "`$HOME/projects/alpha-pilot" "`$HOME/.hermes" 2>/dev/null || true
chmod 700 "`$HOME/.hermes" 2>/dev/null || true
chmod 600 "`$HOME/.hermes/.env" 2>/dev/null || true
cd "`$HOME/projects/alpha-pilot"
python3 -m venv venv-quant
./venv-quant/bin/python -m pip install --upgrade pip
./venv-quant/bin/pip install -r requirements.txt
rm -f data/auto_trader.lock
sudo systemctl daemon-reload
$startBlock
"@
    Invoke-Ssh -HostName $NewHost -User $NewUser -Command $remote | Out-Null
}

function Invoke-Migration {
    Test-LocalPrerequisites
    Test-OciProfile
    Write-Step "验证东京旧机 SSH"
    Wait-SshReady -HostName $OldHost -User $OldUser
    $newHost = Get-NewHostFromState
    Wait-SshReady -HostName $newHost -User $NewUser
    New-RemoteMigrationArchives
    Copy-ArchivesToNewHost -NewHost $newHost
    Install-NewHost -NewHost $newHost
    Save-State @{ migratedAt = (Get-Date).ToString("o") }
}

function Test-NewHost {
    $newHost = Get-NewHostFromState
    Write-Step "验证 PHX 新机运行态: $newHost"
    $units = ($Script:ServiceUnits -join " ")
    $remote = @"
set -uo pipefail
echo "== python =="
python3 --version
echo "== project =="
test -d "`$HOME/projects/alpha-pilot" && echo project-ok
test -f "`$HOME/.hermes/.env" && stat -c '%a %n' "`$HOME/.hermes/.env"
cd "`$HOME/projects/alpha-pilot"
test -x venv-quant/bin/python && venv-quant/bin/python --version
test -f data/quant.db && sqlite3 data/quant.db 'select name from sqlite_master limit 5;' || true
BROKER_MODE=paper venv-quant/bin/python main.py --watchdog || true
echo "== systemd =="
for unit in $units; do
  if systemctl list-unit-files "`$unit" --no-pager 2>/dev/null | grep -q "`$unit"; then
    systemctl --no-pager --full status "`$unit" || true
  fi
done
echo "== ports =="
for port in 5010 5011 5099 9021; do
  curl -fsS --max-time 3 "http://127.0.0.1:`$port/" >/dev/null && echo "port `$port ok" || echo "port `$port no-http"
done
"@
    Invoke-Ssh -HostName $newHost -User $NewUser -Command $remote -AllowFailure | Out-Null
}

function Get-InstancePublicIpInRegion {
    param([string]$Region, [string]$InstanceId)
    $attachments = Invoke-OciJson -Region $Region -Arguments @("compute", "vnic-attachment", "list", "--compartment-id", $CompartmentId, "--instance-id", $InstanceId, "--all")
    foreach ($attachment in $attachments.data) {
        if ($attachment."vnic-id") {
            $vnic = (Invoke-OciJson -Region $Region -Arguments @("network", "vnic", "get", "--vnic-id", $attachment."vnic-id")).data
            if ($vnic."public-ip") {
                return $vnic."public-ip"
            }
        }
    }
    return ""
}

function New-TokyoCleanupInventory {
    Assert-Compartment
    Write-Step "生成东京旧资源清单"
    $ads = Invoke-OciJson -Region $TokyoRegion -Arguments @("iam", "availability-domain", "list", "--compartment-id", $CompartmentId)
    $instances = Invoke-OciJson -Region $TokyoRegion -Arguments @("compute", "instance", "list", "--compartment-id", $CompartmentId, "--all")
    $matchedInstances = @()
    foreach ($instance in $instances.data) {
        if ($instance."lifecycle-state" -eq "TERMINATED") {
            continue
        }
        $publicIp = Get-InstancePublicIpInRegion -Region $TokyoRegion -InstanceId $instance.id
        if ($publicIp -eq $OldHost -or $instance."display-name" -match "quant|pilot") {
            $matchedInstances += [ordered]@{
                id = $instance.id
                displayName = $instance."display-name"
                lifecycleState = $instance."lifecycle-state"
                availabilityDomain = $instance."availability-domain"
                publicIp = $publicIp
            }
        }
    }

    $bootVolumes = @()
    $blockVolumes = @()
    foreach ($ad in $ads.data) {
        $bootList = Invoke-OciJson -Region $TokyoRegion -Arguments @("bv", "boot-volume", "list", "--compartment-id", $CompartmentId, "--availability-domain", $ad.name, "--all")
        foreach ($vol in $bootList.data) {
            if ($vol."lifecycle-state" -ne "TERMINATED" -and $vol."display-name" -match "quant|pilot|Boot Volume") {
                $bootVolumes += $vol
            }
        }
        $volList = Invoke-OciJson -Region $TokyoRegion -Arguments @("bv", "volume", "list", "--compartment-id", $CompartmentId, "--availability-domain", $ad.name, "--all")
        foreach ($vol in $volList.data) {
            if ($vol."lifecycle-state" -ne "TERMINATED" -and $vol."display-name" -match "quant|pilot") {
                $blockVolumes += $vol
            }
        }
    }

    $backups = Invoke-OciJson -Region $TokyoRegion -Arguments @("bv", "backup", "list", "--compartment-id", $CompartmentId, "--all")
    $images = Invoke-OciJson -Region $TokyoRegion -Arguments @("compute", "image", "list", "--compartment-id", $CompartmentId, "--all")
    $reservedIps = Invoke-OciJson -Region $TokyoRegion -Arguments @("network", "public-ip", "list", "--compartment-id", $CompartmentId, "--scope", "REGION", "--all") -AllowFailure

    $inventory = [ordered]@{
        generatedAt = (Get-Date).ToString("o")
        tokyoRegion = $TokyoRegion
        oldHost = $OldHost
        matchedInstances = $matchedInstances
        candidateBootVolumes = @($bootVolumes | Select-Object id, "display-name", "lifecycle-state", "size-in-gbs")
        candidateBlockVolumes = @($blockVolumes | Select-Object id, "display-name", "lifecycle-state", "size-in-gbs")
        candidateVolumeBackups = @($backups.data | Where-Object { $_."display-name" -match "quant|pilot" } | Select-Object id, "display-name", "lifecycle-state")
        candidateCustomImages = @($images.data | Where-Object { $_."display-name" -match "quant|pilot" } | Select-Object id, "display-name", "lifecycle-state")
        reservedPublicIps = @($reservedIps.data | Select-Object id, "display-name", "lifecycle-state", "ip-address")
    }
    $inventory | ConvertTo-Json -Depth 12 | Set-Content -Encoding UTF8 -Path $Script:InventoryFile
    Write-Step "东京清理清单已生成: $Script:InventoryFile"
    Write-Host ($inventory | ConvertTo-Json -Depth 8)
}

function Remove-TokyoResources {
    Assert-Compartment
    if ($ConfirmDelete -ne "DELETE_TOKYO_QUANT_PILOT") {
        throw "删除东京资源必须传入 -ConfirmDelete DELETE_TOKYO_QUANT_PILOT。"
    }
    if (-not (Test-Path $Script:InventoryFile)) {
        New-TokyoCleanupInventory
    }
    $inventory = Get-Content -Raw -Path $Script:InventoryFile | ConvertFrom-Json

    Write-Step "开始删除东京匹配资源"
    foreach ($instance in $inventory.matchedInstances) {
        Write-Step "终止实例并删除 boot volume: $($instance.displayName) $($instance.id)"
        Invoke-OciText -Region $TokyoRegion -Arguments @(
            "compute", "instance", "terminate",
            "--instance-id", $instance.id,
            "--preserve-boot-volume", "false",
            "--force"
        ) | Out-Null
    }

    foreach ($volume in $inventory.candidateBlockVolumes) {
        Write-Step "删除候选 block volume: $($volume.'display-name') $($volume.id)"
        Invoke-OciText -Region $TokyoRegion -Arguments @("bv", "volume", "delete", "--volume-id", $volume.id, "--force") -AllowFailure | Out-Null
    }

    foreach ($backup in $inventory.candidateVolumeBackups) {
        Write-Step "删除候选 volume backup: $($backup.'display-name') $($backup.id)"
        Invoke-OciText -Region $TokyoRegion -Arguments @("bv", "backup", "delete", "--backup-id", $backup.id, "--force") -AllowFailure | Out-Null
    }

    foreach ($image in $inventory.candidateCustomImages) {
        Write-Step "删除候选 custom image: $($image.'display-name') $($image.id)"
        Invoke-OciText -Region $TokyoRegion -Arguments @("compute", "image", "delete", "--image-id", $image.id, "--force") -AllowFailure | Out-Null
    }

    Write-Step "删除完成。建议重新运行 -Phase CleanupInventory 确认东京残留资源。"
}

function Show-Plan {
    Write-Host @"
AlphaPilot OCI PHX 迁移脚本

推荐执行顺序：
  1. 生成/配置 OCI API key：
     pwsh scripts/migrate-oci-phx.ps1 -Phase InitApiKey

  2. 上传 public key 后写入 ~/.oci/config，或重新运行 InitApiKey 并传入：
     -OciUserOcid <ocid1.user...> -OciTenancyOcid <ocid1.tenancy...> -OciFingerprint <fingerprint>

  3. 创建 PHX 网络和 A1 实例：
     pwsh scripts/migrate-oci-phx.ps1 -Phase Provision -CompartmentId <ocid1.compartment-or-tenancy...>

  4. 停东京服务、同步项目/数据/systemd 到 PHX：
     pwsh scripts/migrate-oci-phx.ps1 -Phase Migrate -CompartmentId <ocid1.compartment-or-tenancy...>

  5. 验证 PHX 新机：
     pwsh scripts/migrate-oci-phx.ps1 -Phase Validate -CompartmentId <ocid1.compartment-or-tenancy...>

  6. 生成东京旧资源清单：
     pwsh scripts/migrate-oci-phx.ps1 -Phase CleanupInventory -CompartmentId <ocid1.compartment-or-tenancy...>

  7. 看清单后确认删除东京资源：
     pwsh scripts/migrate-oci-phx.ps1 -Phase CleanupTokyo -CompartmentId <ocid1.compartment-or-tenancy...> -ConfirmDelete DELETE_TOKYO_QUANT_PILOT

状态和日志目录：
  $Script:StatePath
"@
}

switch ($Phase) {
    "Plan" {
        Show-Plan
    }
    "InitApiKey" {
        Initialize-OciApiKey
    }
    "Provision" {
        Test-LocalPrerequisites
        Test-OciProfile
        New-PhxInstance
    }
    "Migrate" {
        Invoke-Migration
    }
    "Validate" {
        Test-LocalPrerequisites
        Test-NewHost
    }
    "CleanupInventory" {
        Test-LocalPrerequisites
        Test-OciProfile
        New-TokyoCleanupInventory
    }
    "CleanupTokyo" {
        Test-LocalPrerequisites
        Test-OciProfile
        Remove-TokyoResources
    }
    "All" {
        Test-LocalPrerequisites
        Test-OciProfile
        New-PhxInstance
        Invoke-Migration
        Test-NewHost
        New-TokyoCleanupInventory
    }
}
