# 清理演示 hosts 映射(需管理员权限),只删除本工具曾写入的行
$ErrorActionPreference = "Stop"
$hostsFile = "$env:SystemRoot\System32\drivers\etc\hosts"
$domains = @("corp-doc.com", "game-site.com", "shop.example",
             "blocked-site.example", "content-check.example")

$lines = Get-Content $hostsFile
$kept = @()
$removed = 0
foreach ($line in $lines) {
    $isDemo = $false
    foreach ($domain in $domains) {
        $pattern = '(?im)^\s*([0-9.]+|\:\:)\s+' + [regex]::Escape($domain) + '\s*(#.*)?$'
        if ($line -match $pattern) {
            $isDemo = $true
            break
        }
    }
    if ($isDemo -or $line.Trim() -eq "# demo mapping (enterprise-proxy)") {
        $removed++
    } else {
        $kept += $line
    }
}
if ($removed -gt 0) {
    Set-Content -Path $hostsFile -Value $kept -Encoding ASCII
    Write-Host "已移除 $removed 行演示映射。"
} else {
    Write-Host "未找到演示映射,无需清理。"
}
ipconfig /flushdns | Out-Null
